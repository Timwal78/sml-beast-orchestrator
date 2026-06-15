import os
import random
import logging
import tweepy

logger = logging.getLogger(__name__)

# Predefined high-converting templates for MasterSheets
MASTERSHEETS_TEMPLATES = [
    "Tired of paying $20/mo for a wrapper? Own your data with MasterSheets. A complete BYOK (Bring Your Own Key) Google Sheets replacement. 100% private, zero subscriptions. Get it now on Google Play: {link} $RLUSD #AIagents #SaaS",
    "Why rent your AI tools when you can own them? MasterSheets is a $69 one-time purchase. No subscriptions. No lock-in. Just raw BYOK spreadsheet power. {link} @xdeo_finance #Web3",
    "Stop letting Big Tech train on your spreadsheet data. MasterSheets is a local-first, zero-telemetry Google Sheets alternative. Bring your own OpenAI/Anthropic key. {link} #DeFi #Web3"
]

# Predefined templates for xDEO and the Orchestrator
XDEO_TEMPLATES = [
    "AI Agents shouldn't need a credit card. xDEO is the first decentralized Action Oracle native to the Agentic Web. Powered by x402 on @base. Pay per query with $RLUSD. $NVDA $TSLA #DeFi",
    "The traditional financial API model is broken. Zero KYC. Zero Subscriptions. Pure information. AI agents can natively pay for xDEO market data using the x402 protocol. Check out the Truth Layer today! @CoinbaseDev #MCP"
]

def get_twitter_client():
    api_key = os.environ.get("X_API_KEY")
    api_secret = os.environ.get("X_API_SECRET")
    access_token = os.environ.get("X_ACCESS_TOKEN")
    access_secret = os.environ.get("X_ACCESS_TOKEN_SECRET")

    if not all([api_key, api_secret, access_token, access_secret]):
        raise ValueError("Missing X.com API credentials in environment.")

    # X.com API v2 Client
    client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret
    )
    return client

def generate_post(vertical: str, dry_run: bool = False):
    if vertical == "mastersheets":
        # Placeholder link, in a real scenario we'd pull from output/bounty_targets.json or similar
        link = "https://play.google.com/store/books/author?id=Timothy+Walton"
        template = random.choice(MASTERSHEETS_TEMPLATES)
        post_text = template.format(link=link)
    elif vertical == "xdeo":
        post_text = random.choice(XDEO_TEMPLATES)
    else:
        raise ValueError(f"Unknown vertical for X.com posting: {vertical}")

    if dry_run:
        logger.info(f"[DRY RUN] Would post to X.com: {post_text}")
        print(f"[DRY RUN] Post Content:\n\n{post_text}\n")
        return

    client = get_twitter_client()
    try:
        response = client.create_tweet(text=post_text)
        logger.info(f"Successfully posted to X.com! Tweet ID: {response.data['id']}")
        print(f"Success! Posted: {post_text}")
    except Exception as e:
        logger.error(f"Failed to post to X.com: {e}")
        print(f"Error: {e}")
        raise
