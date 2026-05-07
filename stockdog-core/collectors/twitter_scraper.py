import asyncio
import logging

logger = logging.getLogger(__name__)

# Note: twscrape requires accounts to be added to its local sqlite database.
# In Oracle Cloud, you will need to run `twscrape add_accounts accounts.txt`
# before this module will work successfully.
try:
    from twscrape import API, gather
    from twscrape.logger import set_log_level
except ImportError:
    logger.warning("twscrape is not installed or failed to load. Twitter scraping will not work.")

async def fetch_recent_tweets(influencer_handles, limit_per_user=5):
    """
    Fetches the most recent tweets from a list of influencers.
    """
    results = {}
    
    try:
        api = API()
        # Suppress overly verbose twscrape logs
        set_log_level("WARNING")
        
        for handle in influencer_handles:
            print(f"Fetching tweets for @{handle}...")
            user_tweets = []
            
            try:
                # We use search instead of user_tweets directly because it's sometimes more reliable
                query = f"from:{handle}"
                tweets = await gather(api.search(query, limit=limit_per_user))
                
                for t in tweets:
                    user_tweets.append({
                        "id": t.id,
                        "date": str(t.date),
                        "content": t.rawContent,
                        "likes": t.likeCount,
                        "retweets": t.retweetCount
                    })
                
                results[handle] = user_tweets
            except Exception as user_e:
                logger.error(f"Failed to fetch tweets for {handle}: {user_e}")
                results[handle] = []
                
    except Exception as e:
        logger.error(f"Twitter API initialization failed: {e}")
        
    return results

def get_influencer_tweets(influencers):
    """
    Synchronous wrapper for the asyncio twscrape logic.
    """
    return asyncio.run(fetch_recent_tweets(influencers))

if __name__ == "__main__":
    import json
    # Test execution (will fail if no accounts are added to twscrape db)
    test_handles = ["garyblack00", "kobeissiletter"]
    print("Warning: This test requires twscrape accounts to be configured.")
    print(json.dumps(get_influencer_tweets(test_handles), indent=2))
