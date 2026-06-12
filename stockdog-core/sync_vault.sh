#!/bin/bash
# Pushes daily Markdown reports to the skyler vault on GitHub.
# Pipeline writes directly to raw/stockdog/daily-market/YYYY-MM-DD/ via Docker volume.

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VAULT_DIR="$DIR/../../skyler"
DATE=$(date +%Y-%m-%d)

# Load shared .env
set -a; source "$DIR/../.env"; set +a

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        -d "text=$1" \
        -d "parse_mode=Markdown" > /dev/null
}

# 주요 일정 (this-week calendar chips) — stage raw/stockdog/calendar/this_week.json
# host-side (preferred wiring, NO docker rebuild). Mirrors publish_m7_tracker /
# publish_macro_tracker: a stdlib+requests host python3 entrypoint writes the
# JSON atomically; we git add the calendar dir. FRED_API_KEY comes from the
# ../.env already sourced at the top of this script. Tolerant: the staging fn
# never raises and writes an `error` field on FRED failure (chips degrade to
# "events: []" → header collapses). Always Mon–Fri of the CURRENT week
# (time-machine static — by design).
stage_calendar() {
    # The module lives in $DIR (stockdog-core); `python3 -m collectors...`
    # needs that as CWD. Run it in a SUBSHELL so the outer CWD ($VAULT_DIR,
    # needed for the git add + later commit) is preserved. Pass an ABSOLUTE
    # vault root so the staging fn writes to the right tree regardless of CWD.
    if ( cd "$DIR" && python3 -m collectors.economic_calendar --stage "$VAULT_DIR" ); then
        git add "raw/stockdog/calendar/"
    else
        echo "[sync_vault.sh] stage_calendar: staging failed — skip (calendar unchanged)"
    fi
}

# IMPR-058 Step 1 — publish today's daily-market reports to the public Garden tree.
# Forward-only: processes ONLY $DATE (today). raw/ is read-only — we copy OUT.
# Layout owned by vault-web build: [2c] flattens 10_Public/daily-reports/ → /daily-reports/,
# [3] strips ![[media/ → ![[ at build time. So we keep reports under 10_Public/daily-reports/
# and leave the media/ prefix in embeds AS-IS (do NOT strip here).
# Idempotent: DEST paths are deterministic; mkdir -p / cp overwrite; re-run never duplicates.
# Assumes CWD is the vault root (set by the `cd "$VAULT_DIR"` above).
publish_to_garden() {
    local pub_dir="10_Public/daily-reports"
    mkdir -p "$pub_dir/media"

    local REGION
    for REGION in US KR; do
        local SRC="raw/stockdog/daily-market/$DATE/Market_Report_${REGION}_${DATE}.md"
        # Forward-only / region-optional: skip silently if today's report for this region is absent.
        [ -f "$SRC" ] || continue

        local region title_word
        region=$(echo "$REGION" | tr '[:upper:]' '[:lower:]')
        case "$REGION" in
            US) title_word="미국" ;;
            KR) title_word="한국" ;;
        esac

        local DEST="$pub_dir/${DATE}-${region}.md"

        # Write public frontmatter, then append the raw body with ONLY the leading
        # frontmatter stripped. awk anchors to the 2nd line that is exactly `---`
        # (NR record of the 2nd boundary), then prints everything strictly after it.
        # This is NOT greedy-to-last: the body contains its own `---` horizontal rules
        # which MUST survive, so we cannot strip "up to the last ---".
        {
            printf -- '---\n'
            printf 'title: "일일 %s 시장 분석 — %s"\n' "$title_word" "$DATE"
            printf 'public: true\n'
            printf 'type: reference\n'
            printf 'date: %s\n' "$DATE"
            printf 'tags:\n'
            printf '  - ctx/public\n'
            printf '  - stockdog\n'
            printf '  - daily-market\n'
            printf '  - region/%s\n' "$region"
            printf -- '---\n'
            awk '
                BEGIN { fm = 0 }
                fm < 2 { if ($0 == "---") fm++; next }
                { print }
            ' "$SRC"
        } > "$DEST"

        # Copy referenced charts/media. Embed text stays AS-IS in DEST.
        # Skip silently if the source media file is missing (e.g. KR reports may have none).
        local embed mediafile msrc
        while IFS= read -r mediafile; do
            [ -n "$mediafile" ] || continue
            msrc="raw/stockdog/daily-market/$DATE/media/$mediafile"
            [ -f "$msrc" ] && cp -f "$msrc" "$pub_dir/media/$mediafile"
        done < <(grep -oE '!\[\[media/[^]]+\]\]' "$SRC" | sed -E 's/^!\[\[media\///; s/\]\]$//')
    done

    # Tightly scoped add — NEVER `git add -A`.
    git add "$pub_dir/"
}

# IMPR-058 Step 3 — render the M7 (insider + short) public tracker page from the
# aggregate dated JSON and stage it. Stdlib-only renderer writes:
#   10_Public/trackers/m7.md   (overwrite; raw/ stays read-only)
# Non-zero exit (no M7 data for $DATE and no fallback) → skip; page unchanged.
# Assumes CWD is the vault root (set by `cd "$VAULT_DIR"` above). git add scoped
# to 10_Public/trackers/ ONLY — never -A.
publish_m7_tracker() {
    local helper="$DIR/render_m7_tracker.py"
    [ -f "$helper" ] || return 0
    if python3 "$helper" "$VAULT_DIR" "$DATE"; then
        git add "10_Public/trackers/"
    else
        echo "[sync_vault.sh] publish_m7_tracker: no M7 data for $DATE — skip (page unchanged)"
    fi
}

# IMPR-061 — render + stage the macro tracker page from the staged macro snapshot.
# Stdlib-only host renderer writes 10_Public/trackers/macro.md (overwrite; raw/
# stays read-only). Non-zero exit (no snapshot staged) → skip; page unchanged.
# git add scoped to 10_Public/trackers/ ONLY — never -A.
publish_macro_tracker() {
    local helper="$DIR/render_macro_tracker.py"
    [ -f "$helper" ] || return 0
    if python3 "$helper" "$VAULT_DIR" "$DATE"; then
        git add "10_Public/trackers/"
    else
        echo "[sync_vault.sh] publish_macro_tracker: no macro snapshot for $DATE — skip (page unchanged)"
    fi
}

# IMPR-062 — render + stage the watchlist tracker page from the staged watchlist
# snapshot. Stdlib-only host renderer writes 10_Public/trackers/watchlist.md
# (overwrite; raw/ stays read-only). Non-zero exit (no snapshot staged) → skip;
# page unchanged. git add scoped to 10_Public/trackers/ ONLY — never -A.
publish_watchlist_tracker() {
    local helper="$DIR/render_watchlist_tracker.py"
    [ -f "$helper" ] || return 0
    if python3 "$helper" "$VAULT_DIR" "$DATE"; then
        git add "10_Public/trackers/"
    else
        echo "[sync_vault.sh] publish_watchlist_tracker: no watchlist snapshot for $DATE — skip (page unchanged)"
    fi
}

# IMPR-063 — render + stage the signals aggregation tracker page. This is a
# READ-ONLY re-aggregation of the SAME snapshots the other trackers consume
# (M7 short/insider, macro, watchlist, F&G); it stages no new raw — only the
# rendered 10_Public/trackers/signals.md. Stdlib-only host renderer (overwrite;
# raw/ stays read-only). Non-zero exit (ALL snapshots missing) → skip; page
# unchanged. Must run AFTER publish_watchlist_tracker (all snapshots fresh).
# git add scoped to 10_Public/trackers/ ONLY — never -A.
# Also stages the signal_count.json gate sidecar (generated by the renderer).
publish_signals_tracker() {
    local helper="$DIR/render_signals_tracker.py"
    [ -f "$helper" ] || return 0
    if python3 "$helper" "$VAULT_DIR" "$DATE"; then
        git add "10_Public/trackers/"
        # Stage the gate sidecar (non-fatal if missing)
        git add "raw/stockdog/signals/" 2>/dev/null || true
    else
        echo "[sync_vault.sh] publish_signals_tracker: no snapshots for $DATE — skip (page unchanged)"
    fi
}

# IMPR-067 — gated automation of the signals "오늘의 읽기" analyst layer.
# Runs the container generator, which is itself GATED on the gate sidecar
# (raw/stockdog/signals/signal_count.json, written by render_signals_tracker.py):
# quiet day → ZERO LLM call. On notable days it injects the analyst read into the
# TODAY_READ block of the ALREADY-staged signals.md.
#
# M1 — publish_signals_tracker staged the PLACEHOLDER block; this hook MUTATES
# signals.md AFTERWARD, so we MUST re-`git add` it or the commit captures the
# placeholder, not the read.
# M2 — `docker compose` needs $DIR (stockdog-core, the compose-file dir), but
# this script runs in $VAULT_DIR (needed for the later git commit). The `cd` is
# isolated in a SUBSHELL so the outer CWD ($VAULT_DIR) is preserved; the re-stage
# `git add` then runs back in $VAULT_DIR.
# M4 — wrapped `{ ...; } || echo` so a generator/docker failure NEVER breaks the
# publish chain (the generator already always exits 0; this is belt-and-braces).
publish_signals_read() {
    local gen="$DIR/generate_signals_read.py"
    [ -f "$gen" ] || return 0
    {
        ( cd "$DIR" && /usr/bin/docker compose run --rm stockdog \
            python generate_signals_read.py /notes "$DATE" )
        git add "10_Public/trackers/signals.md"
    } || echo "[sync_vault.sh] publish_signals_read: skipped (non-fatal)"
}

# IMPR-064 P1 — gated daily narrative generator.
# Writes raw/stockdog/narrative/narrative.json (always — ok or skipped).
# GATED: LLM called only when US daily-market report exists for $DATE AND
# no status:ok narrative already exists for that date (idempotent).
# No-report days → skipped, $0. Already-generated days → skipped, $0.
# M2 — cd into $DIR (compose-file dir) isolated in a subshell; outer CWD preserved.
# M4 — { ...; } || echo so a container/docker failure never breaks the chain.
publish_narrative() {
    local gen="$DIR/generate_narrative.py"
    [ -f "$gen" ] || return 0
    {
        ( cd "$DIR" && /usr/bin/docker compose run --rm stockdog \
            python generate_narrative.py /notes "$DATE" )
        git add "raw/stockdog/narrative/"
    } || echo "[sync_vault.sh] publish_narrative: skipped (non-fatal)"
}

# IMPR-071 D1+D2 — narrative timeline index + detail pages.
# Renders narrative archive into public markdown pages (stdlib-only, no LLM).
# Stages two renderers AFTER publish_narrative (archive must be fresh):
#   1. render_stories_index.py → 10_Public/daily-stories/index.md (all narratives, newest-first)
#   2. render_stories_detail.py → 10_Public/daily-stories/<date>.md (one page per narrative)
# Non-zero exit (no narratives in archive) → skip; pages unchanged.
# git add scoped to 10_Public/daily-stories/ ONLY — never -A.
publish_stories() {
    local idx_helper="$DIR/render_stories_index.py"
    local detail_helper="$DIR/render_stories_detail.py"
    [ -f "$idx_helper" ] && [ -f "$detail_helper" ] || return 0

    if python3 "$idx_helper" "$VAULT_DIR" "$DATE" && \
       python3 "$detail_helper" "$VAULT_DIR"; then
        git add "10_Public/daily-stories/"
    else
        echo "[sync_vault.sh] publish_stories: no narratives to render — skip (pages unchanged)"
    fi
}

cd "$VAULT_DIR" || {
    send_telegram "⚠️ Vault sync failed: skyler directory not found at $VAULT_DIR"
    exit 1
}

# Verify today's report exists
if [ ! -d "raw/stockdog/daily-market/$DATE" ]; then
    echo "Report not found: raw/stockdog/daily-market/$DATE"
    send_telegram "⚠️ Vault sync failed: no report found for $DATE"
    exit 1
fi

# Stage daily report
git add "raw/stockdog/daily-market/$DATE/"

# Stage M7 tracker output (IMPR-044). m7_job.py runs separately and writes:
#   raw/stockdog/m7/insider/$DATE.json + raw/stockdog/m7/short/$DATE.json
#   raw/stockdog/m7/<TICKER>/{insider,short}_{history,latest}.json (×7)
# Missing dirs are harmless — git add silently skips them when m7_job hasn't
# run yet (e.g., emergency disable via config m7.enabled=false).
git add "raw/stockdog/m7/" 2>/dev/null || true

# IMPR-061 — stage the macro snapshot (staged by the US pipeline's
# stage_macro_snapshot). Missing dir is harmless (e.g. FRED skipped) — git add
# silently skips it. raw/ stays read-only; only macro_snapshot.json lives here.
git add "raw/stockdog/macro/" 2>/dev/null || true

# IMPR-062 — stage the watchlist price/volume history + snapshot (written by the
# US pipeline's save_watchlist_day / stage_watchlist_snapshot). Missing dir is
# harmless. raw/ stays read-only; only the watchlist store lives here.
git add "raw/stockdog/watchlist/" 2>/dev/null || true

# 주요 일정 — stage the this-week calendar JSON (after the raw git adds, before
# the garden publish). Host-side staging, no docker rebuild. The dashboard.json
# build (vault-web build_all.sh post-hook) reads this file into payload.calendar.
stage_calendar

# IMPR-058 Step 1 — publish today's reports to the public Garden tree and stage
# the copies so they ride in the same daily commit. Must run AFTER the raw `git add`
# lines and BEFORE `git commit`. raw/ stays read-only (copy OUT only).
publish_to_garden

# IMPR-058 Step 3 — render + stage the M7 tracker page (after publish_to_garden,
# before the diff-cached check so it rides in the same daily commit).
publish_m7_tracker

# IMPR-061 — render + stage the macro tracker page (right after M7, same rules).
publish_macro_tracker

# IMPR-062 — render + stage the watchlist tracker page (right after macro, same rules).
publish_watchlist_tracker

# IMPR-063 — render + stage the signals aggregation tracker (LAST tracker, after
# all snapshots are fresh; read-only re-aggregation — stages no new raw).
publish_signals_tracker

# IMPR-067 — gated analyst "오늘의 읽기" into signals.md (after the tracker render
# so the gate sidecar + placeholder exist; re-stages signals.md). Runs before the
# diff-cached gate so the read rides in the same daily commit.
publish_signals_read

# IMPR-064 P1 — gated daily narrative JSON (after signals_read so all tracker
# pages are current; writes raw/stockdog/narrative/narrative.json).
publish_narrative

# IMPR-071 D1+D2 — narrative timeline index + detail pages (after publish_narrative
# so archive is fresh; stages 10_Public/daily-stories/).
publish_stories

# Skip if nothing changed
if git diff --cached --quiet; then
    echo "Vault sync: nothing new to push."
    exit 0
fi

git commit -m "Daily report $DATE"

# Push to master branch using GITHUB_PAT
if git push "https://${GITHUB_PAT}@github.com/sungho-seo/skyler.git" master 2>&1; then
    send_telegram "📤 *Vault synced*\n\`raw/stockdog/daily-market/$DATE\` → GitHub"
    echo "✅ Vault synced: $DATE"

    # Sprint 1C — vault-web 자동 빌드 트리거 (post-hook).
    # - D-1: push 성공 분기 안에서만 발화 (push 실패 시는 의미 없음).
    # - quality R-3: setsid로 새 세션 분리 → SIGHUP 차단. disown 불필요.
    # - { ... } || true: 빌드 트리거 실패가 stockdog cron 종료 코드 오염 안 시키게.
    # - 호출 그래프 단방향: build_all.sh는 stockdog/sync_vault.sh를 호출하지 않음 → 무한 루프 불가.
    {
        VAULT_WEB_BUILD="/home/ubuntu/service/agents/vault-web/scripts/build_all.sh"
        VAULT_WEB_LOG="/home/ubuntu/service/agents/vault-web/logs/posthook_$(date +%Y%m%d).log"
        if [[ -f "${VAULT_WEB_BUILD}" ]]; then
            echo "[sync_vault.sh] triggering vault-web build_all.sh (post-hook)" >&2
            setsid bash "${VAULT_WEB_BUILD}" \
                </dev/null \
                >>"${VAULT_WEB_LOG}" 2>&1 \
                &
        fi
    } || true
else
    send_telegram "⚠️ Vault sync failed on git push. Check cron_stockdog.log."
    echo "❌ Vault push failed."
    exit 1
fi
