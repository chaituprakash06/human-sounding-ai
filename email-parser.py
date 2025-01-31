from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from supabase import create_client
import os
from dotenv import load_dotenv
import base64
import requests
import time
import re

# Load environment variables
load_dotenv()
required_vars = ['OAUTH_CLIENT_ID', 'OAUTH_CLIENT_SECRET', 'SUPABASE_URL', 'SUPABASE_KEY', 'ALLOWED_DOMAINS']
missing_vars = [var for var in required_vars if not os.getenv(var)]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

print('Environment loaded successfully')

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
ALLOWED_DOMAINS = os.getenv('ALLOWED_DOMAINS', '').split(',')
GPTZERO_API_URL = "https://api.gptzero.me/v2/predict/text"

def count_words(text):
    """Count words in text, handling various whitespace cases"""
    return len(re.findall(r'\b\w+\b', text))

class UsageTracker:
    def __init__(self, limit=300000):
        self.word_count = 0
        self.word_limit = limit
        self.emails_processed = 0
        
    def add_usage(self, text):
        words = count_words(text)
        self.word_count += words
        self.emails_processed += 1
        return words
    
    def get_stats(self):
        return {
            'total_words': self.word_count,
            'percentage_used': (self.word_count / self.word_limit) * 100,
            'words_remaining': self.word_limit - self.word_count,
            'emails_processed': self.emails_processed,
            'average_words_per_email': self.word_count / self.emails_processed if self.emails_processed > 0 else 0
        }

def get_gmail_service():
    client_config = {
        'web': {
            'client_id': os.getenv('OAUTH_CLIENT_ID'),
            'client_secret': os.getenv('OAUTH_CLIENT_SECRET'),
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': ['http://localhost:8080'],
        }
    }
    
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=8080)
    return build('gmail', 'v1', credentials=creds)

def check_email_exists(supabase, message_id):
    """Check if email exists and has GPTZero scores"""
    try:
        response = supabase.table('emails').select('*').eq('message_id', message_id).execute()
        if response.data:
            email = response.data[0]
            has_scores = email.get('gpt_zero_ai') is not None and email.get('gpt_zero_human') is not None
            return True, has_scores
        return False, False
    except Exception as e:
        print(f"Error checking email existence: {str(e)}")
        return False, False

def get_gptzero_scores(text, usage_tracker):
    """Get GPTZero scores and track word usage"""
    words = usage_tracker.add_usage(text)
    print(f"Words in this email: {words}")
    stats = usage_tracker.get_stats()
    print(f"Total words processed: {stats['total_words']:,}")
    print(f"Percentage of limit used: {stats['percentage_used']:.1f}%")
    
    headers = {
        'Content-Type': 'application/json'
    }
    
    data = {
        "document": text,
        "multilingual": False
    }
    
    try:
        response = requests.post(GPTZERO_API_URL, headers=headers, json=data)
        if response.status_code == 200:
            result = response.json()
            scores = result['documents'][0]['class_probabilities']
            return scores.get('ai', 0), scores.get('human', 0)
        elif response.status_code == 429:
            print("Rate limit reached for GPTZero API")
            return None, None
        else:
            print(f"GPTZero API error: {response.status_code}")
            return None, None
    except Exception as e:
        print(f"Error calling GPTZero API: {str(e)}")
        return None, None

def get_email_body(message):
    if 'parts' in message['payload']:
        parts = message['payload']['parts']
        for part in parts:
            if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                return base64.urlsafe_b64decode(part['body']['data']).decode()
    elif 'body' in message['payload'] and 'data' in message['payload']['body']:
        return base64.urlsafe_b64decode(message['payload']['body']['data']).decode()
    return ''

def upsert_email(supabase, email_data):
    try:
        result = supabase.table('emails').upsert(
            email_data,
            on_conflict='message_id'
        ).execute()
        return result
    except Exception as e:
        print(f"Supabase upsert error: {str(e)}")
        return None

def main():
    try:
        usage_tracker = UsageTracker()
        gmail = get_gmail_service()
        supabase = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))
        print("Services initialized successfully")

        query = ' OR '.join(f'from:{domain}' for domain in ALLOWED_DOMAINS)
        results = gmail.users().messages().list(userId='me', q=query, maxResults=50).execute()
        messages = results.get('messages', [])
        
        if not messages:
            print("No matching emails found")
            return
            
        print(f"Found {len(messages)} emails to process")
        
        stats = {
            'processed': 0,
            'skipped_existing': 0,
            'skipped_rate_limit': 0,
            'errors': 0
        }
        
        for i, message_meta in enumerate(messages, 1):
            try:
                message_id = message_meta['id']
                exists, has_scores = check_email_exists(supabase, message_id)
                
                if exists and has_scores:
                    print(f"\nEmail {i}/{len(messages)} - SKIPPED (already processed)")
                    stats['skipped_existing'] += 1
                    continue
                
                message = gmail.users().messages().get(userId='me', id=message_id, format='full').execute()
                headers = message['payload']['headers']
                sender = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
                body = get_email_body(message)
                
                if not body:
                    print(f"\nEmail {i}/{len(messages)} - SKIPPED (no content)")
                    stats['errors'] += 1
                    continue

                print(f"\nEmail {i}/{len(messages)} - Processing...")
                print(f"From: {sender}")
                
                # Check word count before making API call
                words = count_words(body)
                total_after = usage_tracker.word_count + words
                if total_after > usage_tracker.word_limit:
                    print(f"WARNING: Processing this email would exceed the word limit")
                    print(f"Current total: {usage_tracker.word_count:,}")
                    print(f"This email: {words:,} words")
                    print(f"Would exceed limit by: {total_after - usage_tracker.word_limit:,} words")
                    break

                email_data = {
                    'message_id': message_id,
                    'sender': sender,
                    'body': body
                }

                if not has_scores:
                    ai_score, human_score = get_gptzero_scores(body, usage_tracker)
                    if ai_score is not None and human_score is not None:
                        email_data.update({
                            'gpt_zero_ai': ai_score,
                            'gpt_zero_human': human_score
                        })
                        stats['processed'] += 1
                    else:
                        stats['skipped_rate_limit'] += 1

                result = upsert_email(supabase, email_data)
                if result:
                    action = 'Updated' if exists else 'Inserted'
                    scores_msg = f" (AI: {ai_score:.2f}, Human: {human_score:.2f})" if 'gpt_zero_ai' in email_data else " (without scores)"
                    print(f"✓ {action} email{scores_msg}")
                
                time.sleep(2)
                
            except Exception as e:
                print(f"Error processing email {i}: {str(e)}")
                stats['errors'] += 1
                continue
        
        # Print final stats
        print("\nProcessing Complete!")
        print(f"Processed: {stats['processed']}")
        print(f"Skipped (existing): {stats['skipped_existing']}")
        print(f"Skipped (rate limit): {stats['skipped_rate_limit']}")
        print(f"Errors: {stats['errors']}")
        
        # Print usage stats
        final_stats = usage_tracker.get_stats()
        print("\nWord Usage Statistics:")
        print(f"Total words processed: {final_stats['total_words']:,}")
        print(f"Words remaining in limit: {final_stats['words_remaining']:,}")
        print(f"Percentage of limit used: {final_stats['percentage_used']:.1f}%")
        print(f"Average words per email: {final_stats['average_words_per_email']:.0f}")
        
    except Exception as e:
        print(f"Fatal error: {str(e)}")
        raise

if __name__ == '__main__':
    main()