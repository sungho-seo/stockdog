import os
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

def save_to_obsidian(content, config):
    """
    Wraps the LLM content with Obsidian frontmatter tags and saves it to the designated vault directory.
    """
    try:
        vault_dir = config.get("obsidian", {}).get("vault_output_dir", "../notes/daily-market")
        date_format = config.get("obsidian", {}).get("date_format", "%Y-%m-%d")
        
        # Ensure the directory exists
        # In production on Oracle, this should point to the git clone of the 'skyler' private repo
        os.makedirs(vault_dir, exist_ok=True)
        
        # Generate filename based on current date
        current_date_str = datetime.now().strftime(date_format)
        filename = f"Market_Report_{current_date_str}.md"
        filepath = os.path.join(vault_dir, filename)
        
        # Add metadata (frontmatter) for Obsidian
        frontmatter = f"---\ntags:\n  - market-report\n  - stockdog\ndate: {current_date_str}\n---\n\n"
        
        final_content = frontmatter + content
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(final_content)
            
        print(f"✅ Successfully saved Markdown report to {filepath}")
        return filepath
    except Exception as e:
        logger.error(f"Failed to save Markdown file: {e}")
        return None

def save_raw_twitter_data(twitter_data, config):
    """
    Saves raw Twitter data as a Markdown file for transparency and record keeping.
    """
    try:
        raw_dir = config.get("obsidian", {}).get("raw_output_dir", "../notes/daily-market/raw")
        date_format = config.get("obsidian", {}).get("date_format", "%Y-%m-%d")
        
        os.makedirs(raw_dir, exist_ok=True)
        
        current_date_str = datetime.now().strftime(date_format)
        filename = f"Raw_Twitter_{current_date_str}.md"
        filepath = os.path.join(raw_dir, filename)
        
        content = f"---\ntags:\n  - raw-data\n  - twitter\ndate: {current_date_str}\n---\n\n"
        content += f"# Raw Twitter Data Collection ({current_date_str})\n\n"
        
        for handle, tweets in twitter_data.items():
            content += f"## @{handle}\n"
            if not tweets:
                content += "No tweets collected or error fetching.\n\n"
                continue
                
            for tweet in tweets:
                content += f"- **Date**: {tweet.get('date', 'Unknown')}\n"
                content += f"- **Stats**: {tweet.get('likes', 0)} Likes, {tweet.get('retweets', 0)} Retweets\n"
                content += f"- **Content**:\n  > {tweet.get('content', '').replace(chr(10), chr(10) + '  > ')}\n\n"
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
            
        print(f"✅ Successfully saved raw Twitter data to {filepath}")
        return filepath
    except Exception as e:
        logger.error(f"Failed to save raw Twitter data: {e}")
        return None
