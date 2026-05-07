import os
import requests
import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

GETXAPI_KEY = os.getenv("GETXAPI_KEY")

def get_influencer_tweets(influencer_handles, limit_per_user=5):
    """
    Fetches the most recent tweets from a list of influencers using GetXAPI.
    """
    if not GETXAPI_KEY:
        logger.warning("GETXAPI_KEY is not set in .env. Twitter scraping will not work.")
        return {handle: [] for handle in influencer_handles}

    results = {}
    headers = {"Authorization": f"Bearer {GETXAPI_KEY}"}
    
    for handle in influencer_handles:
        print(f"Fetching tweets for @{handle}...")
        user_tweets = []
        
        try:
            params = {
                "q": f"from:{handle}",
                "product": "Latest"
            }
            
            response = requests.get(
                "https://api.getxapi.com/twitter/tweet/advanced_search",
                headers=headers,
                params=params,
                timeout=15
            )
            
            if response.status_code == 200:
                data = response.json()
                tweets = data.get("tweets", [])
                
                # Limit the number of tweets
                tweets = tweets[:limit_per_user]
                
                for t in tweets:
                    user_tweets.append({
                        "id": t.get("id_str", t.get("id", "")),
                        "date": t.get("createdAt", ""),
                        "content": t.get("text", ""),
                        "likes": t.get("favorite_count", t.get("likeCount", t.get("likes", 0))),
                        "retweets": t.get("retweet_count", t.get("retweetCount", t.get("retweets", 0)))
                    })
            else:
                logger.error(f"GetXAPI error for {handle}: {response.status_code} - {response.text}")
                
            results[handle] = user_tweets
            
        except Exception as e:
            logger.error(f"Failed to fetch tweets for {handle}: {e}")
            results[handle] = []
            
    return results

if __name__ == "__main__":
    import json
    # Test execution
    test_handles = ["garyblack00", "kobeissiletter"]
    print(json.dumps(get_influencer_tweets(test_handles), indent=2))
