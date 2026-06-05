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

# IMPR-058 Step 1 — publish today's reports to the public Garden tree and stage
# the copies so they ride in the same daily commit. Must run AFTER the raw `git add`
# lines and BEFORE `git commit`. raw/ stays read-only (copy OUT only).
publish_to_garden

# IMPR-058 Step 3 — render + stage the M7 tracker page (after publish_to_garden,
# before the diff-cached check so it rides in the same daily commit).
publish_m7_tracker

# IMPR-061 — render + stage the macro tracker page (right after M7, same rules).
publish_macro_tracker

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
