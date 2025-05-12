import asyncio
import re # Import regex module
import argparse # ADDED: For command-line arguments
from playwright.async_api import async_playwright
import time # Import time for sleep

# Helper function to clean text
def clean_text(text):
    if not text: return "N/A"
    # Remove common unicode icons and excessive whitespace
    text = re.sub(r'[\ue000-\uf8ff]', '', text) # Remove characters in Private Use Area (common for icons)
    text = ' '.join(text.split()) # Normalize whitespace
    return text.strip()

# Helper function to remove substring ONCE based on match object span
def remove_match_from_string(text, match_obj):
    if not text or not match_obj:
        return text
    start, end = match_obj.span()
    return text[:start] + text[end:]

async def scrape_google_maps(search_query: str):
    """
    Scrapes Google Maps for a given search query, handling scrolling and extracting detailed data using page.evaluate for efficiency.
    """
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        # --- REMOVED: Resource Blocking ---
        # await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font", "media"] else route.continue_())
        # print("Resource blocking enabled for images, fonts, media.")
        # -------------------------------

        # ADDED: Short delay before navigation to ensure page context is ready
        await asyncio.sleep(1)

        print(f"Navigating to Google Maps...")
        await page.goto("https://www.google.com/maps")

        # --- Consent form handling (may vary by region/time) ---
        # Simple example: looking for a button with text like "Accept all"
        try:
            print("Checking for consent form...")
            await page.locator('button:has-text("Accept all"), button:has-text("Reject all"), button:has-text("I agree")').first.click(timeout=3000) # Shorter timeout
            print("Consent form handled.")
        except Exception as e:
            # Don't print full error if it's just a timeout (common)
            if "timeout" in str(e).lower():
                 print(f"Consent form not found or timed out (likely okay). ")
            else:
                 print(f"Consent form interaction error: {e}")

        print(f"Searching for: {search_query}")
        await page.locator('#searchboxinput').fill(search_query)
        # Use keyboard Enter press instead of button click, sometimes more reliable
        await page.locator('#searchboxinput').press('Enter')

        # Wait for search results to appear (adjust selector and timeout as needed)
        print("Waiting for search results...")
        results_panel_selector = 'div[role="feed"]'
        try:
            await page.locator(results_panel_selector).first.wait_for(state="visible", timeout=60000)
            print("Search results loaded.")
        except Exception as e:
            print(f"Failed to load search results or timed out: {e}")
            await browser.close()
            return [] # Return empty list on failure

        # --- Scrolling Logic ---
        print("Scrolling to load all results...")
        scrollable_element = page.locator(results_panel_selector).first
        result_item_selector = 'div.Nv2PK' 

        previous_results_count = 0
        scroll_attempts = 0
        max_scroll_attempts = 20 
        consecutive_no_change = 0 

        while scroll_attempts < max_scroll_attempts:
            current_results_count = await page.locator(result_item_selector).count()
            print(f"Found {current_results_count} results so far...")

            if current_results_count == previous_results_count and scroll_attempts > 0:
                consecutive_no_change += 1
                # Check for end marker (using .first for safety)
                end_marker = page.locator('span:has-text("You\\\'ve reached the end of the list.")').first
                if await end_marker.is_visible(timeout=1000): # Short timeout check
                    print("Reached the end of the list marker.")
                    break

                if consecutive_no_change >= 3: # Stop if count hasn't changed for 3 scrolls
                    print(f"Result count unchanged for {consecutive_no_change} scrolls. Assuming end.")
                    break
                else:
                    print(f"Result count unchanged ({consecutive_no_change} time(s)), waiting briefly...")
                    await asyncio.sleep(1.5 + consecutive_no_change) 
                    # Scroll again
                    try:
                       await scrollable_element.evaluate('(element) => element.scrollTop = element.scrollHeight')
                       await asyncio.sleep(1.0)
                    except Exception as scroll_err:
                       print(f"  Error during scroll attempt: {scroll_err}")
                       break # Exit scroll loop if scrolling fails
            else:
                consecutive_no_change = 0 # Reset counter

            previous_results_count = current_results_count
            try:
                await scrollable_element.evaluate('(element) => element.scrollTop = element.scrollHeight')
                await asyncio.sleep(1.5) # Standard wait after scroll
            except Exception as scroll_err:
                print(f"  Error during scroll attempt: {scroll_err}")
                break # Exit scroll loop if scrolling fails
                
            scroll_attempts += 1
            if scroll_attempts >= max_scroll_attempts:
                print("Reached maximum scroll attempts.")


        # --- Data Extraction (Refactored with page.evaluate) ---
        print(f"Scrolling finished. Extracting data from {previous_results_count} results...")
        result_elements = await page.locator(result_item_selector).all()

        # JavaScript function to extract basic data from a result element
        # This function runs in the browser context
        js_extractor = """
        (element) => {
            const data = {
                name: null,
                rating_text: null, // Raw text like "4.5 stars" or just number
                reviews_text: null, // Raw text like "(1,234)" - Parentheses included!
                website_url: null, // ADDED Website URL
                // REMOVED: info_text - will fetch separately in Python
            };

            // 1. Extract Name (using aria-label)
            try {
                data.name = element.querySelector('a[aria-label]')?.getAttribute('aria-label');
            } catch (e) { /* Ignore JS Error (Name) */ }

            // 2. Extract Rating/Review text (using potential selectors)
            try {
                const ratingSpan = element.querySelector('span[aria-label*="stars"]');
                if (ratingSpan) {
                   const fullRatingLabel = ratingSpan.ariaLabel || '';
                   const ratingMatch = fullRatingLabel.match(/(\d\.\d)/);
                   if (ratingMatch) data.rating_text = ratingMatch[1]; // Just the number
                   
                   // Check sibling or parent for review count in standard format (e.g., next span)
                   const reviewSpan = ratingSpan.nextElementSibling; 
                   if (reviewSpan && reviewSpan.textContent.match(/^\s*\(\s*\d{1,3}(?:[,.]\d{3})*\s*\)\s*$/)) {
                       data.reviews_text = reviewSpan.textContent.trim(); // Get the text with parens
                   } else {
                       // Sometimes review count is inside the rating span's label itself
                       const reviewMatchLabel = fullRatingLabel.match(/(\d{1,3}(?:[,.]\d{3})*)\s+reviews?/i);
                       if (reviewMatchLabel) {
                          data.reviews_text = '(' + reviewMatchLabel[1] + ')'; // Reconstruct standard format
                       }
                   }
                }
            } catch (e) { /* Ignore JS Error (Rating/Review) */ }
            
            // ADDED: Attempt to find website URL
            try {
                // Look for a button or link with aria-label="Website" or specific data-item-id
                const websiteLink = element.querySelector('a[aria-label="Website"], a[data-item-id="authority"]');
                if (websiteLink && websiteLink.href && !websiteLink.href.startsWith('https://www.google.com/maps')) {
                    data.website_url = websiteLink.href;
                }
            } catch (e) { /* Ignore JS Error (Website) */ }
            
            return data;
        }
        """

        for i, element in enumerate(result_elements):
            print(f"--- Processing result item {i+1}/{len(result_elements)} ---")
            
            # Call the JavaScript extractor function for basic fields
            try:
                extracted_data = await element.evaluate(js_extractor)
            except Exception as eval_err:
                print(f"  Error evaluating element {i+1}: {eval_err}")
                continue 

            # Initialize Python fields
            name, rating, reviews, price_str, place_type, address, phone_number, website_url = ["N/A"] * 8
            
            # Use data returned from JavaScript
            name = clean_text(extracted_data.get('name'))
            rating = extracted_data.get('rating_text') or "N/A" # Rating is directly from JS now
            reviews = clean_text(extracted_data.get('reviews_text')) or "N/A" # Reviews are directly from JS now
            website_url = extracted_data.get('website_url') # Get from JS
            
            # --- Fetch Info Block Text using Python Locator ---
            full_info_text_original = "" 
            try:
                info_elements = await element.locator('div.fontBodyMedium').all()
                info_texts = [await el.text_content() for el in info_elements]
                full_info_text_original = clean_text(' · '.join(filter(None, info_texts)))
                print(f"  Fetched Info Block Text: '{full_info_text_original}'")
            except Exception as info_err:
                print(f"  Error fetching info block text: {info_err}")
            
            # Log initial data
            print(f"  Name (JS): {name}")
            print(f"  Rating (JS): {rating}")
            print(f"  Reviews (JS): {reviews}")
            print(f"  Website (JS): {website_url}") # Log JS website

            # If name extraction failed in JS, skip item
            if not name or name == "N/A":
                print(f"  Skipping item {i+1} due to missing name.")
                continue
            
            # Start Python processing with the separately fetched info text
            working_info_text = full_info_text_original 

            try:
                # --- Python Regex Processing --- 
                
                # Remove rating/review numbers if they appear verbatim in the info text
                # (They shouldn't typically, but as a safety measure)
                if rating != "N/A" and rating in working_info_text:
                    working_info_text = working_info_text.replace(rating, "", 1)
                if reviews != "N/A" and reviews in working_info_text:
                    working_info_text = working_info_text.replace(reviews, "", 1)
                
                working_info_text = clean_text(working_info_text)

                # --- Price Extraction (Simplified Logic) ---
                print(f"    Text for Price: '{working_info_text}'")
                price_str = "N/A" # Default
                price_match_broad = None

                # Pattern 1: Try symbol followed by numbers/range/plus (potentially greedy)
                price_regex_num_broad = r'([£$€₹¥฿][\s]*[\d,.-–+]+)' 
                price_match_broad = re.search(price_regex_num_broad, working_info_text, re.IGNORECASE)
                
                if price_match_broad:
                    potential_price_str = price_match_broad.group(1) # e.g., "€20–30French"
                    print(f"    Price Matched (Broad): '{potential_price_str}'")
                    
                    # Pattern 1a: Extract ONLY the valid price part from the start of the broad match
                    # Revised cleaning regex to be more specific about valid price structures
                    price_regex_num_clean = r'^([£$€₹¥฿][\s]*(?:[\d.,]+(?:[–-][\d.,]+)?|[£$€₹¥฿]{0,2})\+?)' # More structured cleaning
                    price_match_clean = re.match(price_regex_num_clean, potential_price_str, re.IGNORECASE) # Match from start
                    if price_match_clean:
                        price_str = clean_text(price_match_clean.group(1))
                        print(f"    Price Extracted (Cleaned Num): '{price_str}'")
                        working_info_text = remove_match_from_string(working_info_text, price_match_broad) # Remove original broad match
                    else:
                         print(f"    Cleaned Num Extraction Failed for: '{potential_price_str}'")
                         # Fall through to symbol check if clean extraction fails

                # Fallback / Pattern 2: If numeric pattern didn't yield a clean price, try symbol-only
                if price_str == "N/A":
                    price_regex_sym = r'([£$€₹¥฿]{1,4})'
                    price_match_sym = re.search(price_regex_sym, working_info_text)
                    if price_match_sym:
                        price_str = clean_text(price_match_sym.group(1))
                        print(f"    Price Matched (Sym): '{price_match_sym.group(0)}', Extracted: '{price_str}'")
                        working_info_text = remove_match_from_string(working_info_text, price_match_sym)

                # 4. Extract Phone Number (from original text - unchanged)
                phone_regex = r'(\+\d{1,4}[ \-]?(\(?\d{2,4}\)?|[\d]{2,4})[ \-]?\d{3,}[\s-]?\d{3,})'
                phone_match = re.search(phone_regex, full_info_text_original) # Use original text here
                if phone_match:
                    phone_number = clean_text(phone_match.group(1))
                    print(f"    Phone Matched: '{phone_number}'")
                    # Remove from working text if present
                    try:
                       working_info_text = re.sub(re.escape(phone_match.group(0)), '', working_info_text, count=1)
                    except re.error:
                        working_info_text = working_info_text.replace(phone_match.group(0), "")
                   
                # Clean working text
                working_info_text = clean_text(working_info_text.replace('· ·', '·').strip(' ·'))
                print(f"  Text for Type/Address: '{working_info_text}'")

                # 5. Extract Type
                # Reordered Known Types (more specific first) + Re-added Word Boundaries
                known_types = [
                    # More specific first
                    "Used book store", "Comic book store", "Rare book store", 
                    # General bookstore
                    "Book store", 
                    # Other common types
                    "Restaurant", "Cafe", "Bar", "Pub", "Hotel", "Steakhouse", "Steak", 
                    "Chophouse", "Pakistani", "Indian", "Chinese", "Italian", "Thai", 
                    "Japanese", "Mexican", "Greek", "Turkish", "Lebanese", "Brunch", 
                    "Bakery", "Dessert", "Coffee", "Tea", "Fast Food", "Fine Dining", "Barbecue"
                ]
                type_found = False
                for k_type in known_types:
                    try:
                        # RE-ADDED word boundaries \b for accuracy
                        type_match_obj = re.search(r'\\b{}\\b'.format(re.escape(k_type)), working_info_text, re.IGNORECASE)
                        if type_match_obj:
                            place_type = k_type # Use the canonical form
                            print(f"    Type Found (Keyword): '{place_type}', Match: '{type_match_obj.group(0)}'")
                            working_info_text = remove_match_from_string(working_info_text, type_match_obj)
                            type_found = True
                            break
                    except re.error:
                        print(f"    Regex error matching type: {k_type}")
                        continue
                       
                if not type_found:
                    print(f"    Type not found via keywords.")

                # Clean text again
                working_info_text = clean_text(working_info_text.strip(' ·'))
                print(f"  Text for Address: '{working_info_text}'")

                # 6. Extract Address (Remaining text - unchanged)
                if working_info_text:
                    address = re.sub(r'(\\s*·\\s*)?(Open|Closed|Closes|Opening|Hours|Serves|Delivers|Takeout|Dine-in|Pickup|Delivery|Offers|Ends|Starts|Temporary|Permanently).*$', '', working_info_text, flags=re.IGNORECASE).strip()
                    address = clean_text(address)
                    if not address or len(address) < 5: 
                        address = "N/A" 
               
                # Final sanity checks (unchanged)
                if place_type == address : place_type = "N/A" 
                if not place_type or place_type.isdigit(): place_type = "N/A"
                if not address or address.isdigit(): address = "N/A"
                if not website_url: website_url = "N/A" # Ensure N/A if not found

            except Exception as e:
                print(f"  Error during detailed Python info extraction: {e}")
                # Ensure all fields have a default
                name, rating, reviews, price_str, place_type, address, phone_number, website_url = \
                    map(lambda x: x if x != "N/A" else "N/A", \
                        [name, rating, reviews, price_str, place_type, address, phone_number, website_url])

            result_data = {
                "Name": name, 
                "Rating": rating,
                "Reviews": reviews,
                "Price": price_str,
                "Type": place_type,
                "Address": address,
                "Phone Number": phone_number,
                "Website": website_url
            }
            results.append(result_data)
            print(f"  -> Appended: {result_data}")

        print(f"\nExtraction complete. Appended data for {len(results)} places.")
        await browser.close()
        return results

async def main():
    # --- Argument Parsing ---
    parser = argparse.ArgumentParser(description="Scrape Google Maps for a search query.")
    parser.add_argument("search_query", help="The search query (e.g., 'restaurants in London', 'cafes in Paris')")
    args = parser.parse_args()

    search_query = args.search_query 
    print(f"Starting scraper for: {search_query}")

    scraped_data = await scrape_google_maps(search_query)

    if scraped_data:
        print("\n--- Extracted Data ---")
        try:
            import pandas as pd
            df = pd.DataFrame(scraped_data, columns=["Name", "Rating", "Reviews", "Price", "Type", "Address", "Phone Number", "Website"])
            print(df.to_string())
        except ImportError:
            print("Pandas not installed. Printing manually:")
            for i, place in enumerate(scraped_data):
                 print(f"\n--- Result {i+1} ---")
                 for key in ["Name", "Rating", "Reviews", "Price", "Type", "Address", "Phone Number", "Website"]:
                     value = place.get(key, "N/A") 
                     print(f"  {key}: {value}")
        print(f"\n----------------------\nTotal: {len(scraped_data)} places")
    else:
        print("No data was extracted or appended.")

if __name__ == "__main__":
    asyncio.run(main()) 