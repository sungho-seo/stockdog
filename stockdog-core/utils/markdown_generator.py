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
