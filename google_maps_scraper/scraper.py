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
    Scrapes Google Maps for a given search query, handling scrolling and extracting detailed data.
    """
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

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
            await page.locator(results_panel_selector).first.wait_for(state="visible", timeout=30000)
            print("Search results loaded.")
        except Exception as e:
            print(f"Failed to load search results or timed out: {e}")
            await browser.close()
            return [] # Return empty list on failure

        # --- Scrolling Logic ---
        print("Scrolling to load all results...")
        scrollable_element = page.locator(results_panel_selector).first
        # Selector for individual result items (needs careful inspection/adjustment)
        result_item_selector = 'div.Nv2PK' # Try a common class name for result items

        previous_results_count = 0
        scroll_attempts = 0
        max_scroll_attempts = 20 # Slightly increased max scrolls
        consecutive_no_change = 0 # Track consecutive scrolls with no new results

        while scroll_attempts < max_scroll_attempts:
            current_results_count = await page.locator(result_item_selector).count()
            print(f"Found {current_results_count} results so far...")

            if current_results_count == previous_results_count and scroll_attempts > 0:
                consecutive_no_change += 1
                end_marker_visible = await page.locator('span:has-text("You\'ve reached the end of the list.")').first.is_visible()
                if end_marker_visible:
                    print("Reached the end of the list marker.")
                    break

                if consecutive_no_change >= 3: # Stop if count hasn't changed for 3 scrolls
                    print(f"Result count unchanged for {consecutive_no_change} scrolls. Assuming end.")
                    break
                else:
                    print(f"Result count unchanged ({consecutive_no_change} time(s)), waiting briefly...")
                    await asyncio.sleep(1.5 + consecutive_no_change) # Slightly adjusted wait
                    await scrollable_element.evaluate('(element) => element.scrollTop = element.scrollHeight')
                    await asyncio.sleep(1.0) # Shorter wait after this specific scroll
            else:
                # Reset counter if results were found
                consecutive_no_change = 0

            previous_results_count = current_results_count
            await scrollable_element.evaluate('(element) => element.scrollTop = element.scrollHeight')
            # Wait for potential new results to load after scroll
            await asyncio.sleep(1.5) # Standard wait after scroll
            scroll_attempts += 1
            if scroll_attempts >= max_scroll_attempts:
                print("Reached maximum scroll attempts.")


        # --- Data Extraction ---
        print(f"Scrolling finished. Extracting data from {previous_results_count} results...")
        result_elements = await page.locator(result_item_selector).all()

        for i, element in enumerate(result_elements):
            print(f"--- Processing result item {i+1}/{len(result_elements)} ---")
            # Initialize fields
            name, rating, reviews, price_str, place_type, address, phone_number = ["N/A"] * 7
            price_match_text, type_match_text, address_match_text = "", "", ""

            # --- Name Extraction --- (Using the reliable aria-label selector)
            name_selector = 'a[aria-label]'
            try:
                name_element = element.locator(name_selector).first
                name = await name_element.get_attribute('aria-label')
                name = clean_text(name)
                print(f"  Name found: {name}")
            except Exception as e:
                print(f"  Could not extract name: {e}")
                name = "N/A"
                # If name extraction fails, skip item - unlikely to get other fields correctly
                continue

            # Extract from all info containers combined for better matching
            info_container_selector = 'div.fontBodyMedium'
            full_info_text_original = ""
            try:
                info_elements = await element.locator(info_container_selector).all()
                all_texts = []
                for info_el in info_elements:
                    text_content = await info_el.text_content()
                    if text_content:
                        all_texts.append(clean_text(text_content))
                full_info_text_original = " · ".join(all_texts) # Join with separator for parsing
                print(f"  Raw Info Text: '{full_info_text_original}'")
            except Exception as e:
                print(f"  Error reading info containers: {e}")
                # Continue processing even if text reading fails, might get name only

            working_info_text = full_info_text_original

            try:
                # 1. Extract Rating
                rating_match = re.search(r'(\d\.\d)', working_info_text)
                if rating_match:
                    rating = rating_match.group(1)
                    # Safely remove from working text
                    working_info_text = remove_match_from_string(working_info_text, rating_match)

                # 2. Extract Reviews
                reviews_match = re.search(r'(\(\s*\d{1,3}(?:[,.]\d{3})*\s*\))', working_info_text)
                if reviews_match:
                    reviews = clean_text(reviews_match.group(1))
                    working_info_text = remove_match_from_string(working_info_text, reviews_match)

                # 3. Extract Price (Revised Logic - Swapped Order)
                print(f"    Text for Price: '{working_info_text}'")
                price_str = "N/A" # Default
                price_match = None

                # Pattern 1: Try symbol followed by numbers/range first (e.g., £20-40, $100+)
                # Reverted: Broader match, will rely on post-processing
                price_regex_num = r'([£$€₹¥฿][\s]*[\d,.-–+]+)' # Symbol, optional space, digits/commas/dots/hyphens/en-dash/plus
                price_match = re.search(price_regex_num, working_info_text, re.IGNORECASE)
                if price_match:
                    potential_price = clean_text(price_match.group(1))
                    print(f"    Price Matched (Num - Raw): '{price_match.group(0)}', Potential: '{potential_price}'")
                    # Post-processing: Trim trailing non-numeric/non-symbol chars (like letters)
                    # Revised cleaning regex to be more specific about valid price structures
                    cleaned_price_match = re.match(r'([£$€₹¥฿][\s]*(?:[\d.,]+(?:[–-][\d.,]+)?|[£$€₹¥฿]{0,2})\+?)', potential_price)
                    if cleaned_price_match:
                        # Check if the entire potential_price was matched by the cleaning regex or if there are trailing characters
                        if cleaned_price_match.group(0) == potential_price or potential_price[len(cleaned_price_match.group(0)):].isspace():
                            price_str = cleaned_price_match.group(1).strip()
                            print(f"    Price Post-Processed (Clean Match): '{price_str}'")
                        else:
                            # The cleaning regex matched a part, but there was other non-space stuff after it in potential_price
                            # This case implies the initial broad match was too greedy and included non-price text
                            # For example, potential_price = "£20-30Italian", cleaned_match = "£20-30"
                            # We should use the cleaned match.
                            price_str = cleaned_price_match.group(1).strip()
                            print(f"    Price Post-Processed (Partial Cleaned): '{price_str}', Trailing: '{potential_price[len(cleaned_price_match.group(0)):]}'")
                    else:
                        price_str = "N/A" # If cleaning fails, reset
                        print(f"    Price Post-Processing Failed for: '{potential_price}'")
                else:
                    # Pattern 2: Try symbol-only as fallback (e.g., $, ££, $$$)
                    price_regex_sym = r'([£$€₹¥฿]{1,4})' # Match 1 to 4 currency symbols 
                    price_match = re.search(price_regex_sym, working_info_text)
                    if price_match:
                        price_str = clean_text(price_match.group(1))
                        # No complex post-processing needed here, symbols are simple
                        print(f"    Price Matched (Sym): '{price_match.group(0)}', Extracted: '{price_str}'")

                # Final cleanup removed, handled in post-processing logic above

                if price_match and price_str != "N/A":
                    # Use the original match object span for removal, even if we post-processed the string
                    working_info_text = remove_match_from_string(working_info_text, price_match)

                # 4. Extract Phone Number (from original text)
                phone_regex = r'(\+\d{1,4}[ \-]?(\(?\d{2,4}\)?|[\d]{2,4})[ \-]?\d{3,}[\s-]?\d{3,})'
                phone_match = re.search(phone_regex, full_info_text_original)
                if phone_match:
                    phone_number = clean_text(phone_match.group(1))
                    print(f"    Phone Matched: '{phone_number}'")
                    # Also try to remove from working text, carefully
                    try:
                       working_info_text = re.sub(re.escape(phone_match.group(0)), '', working_info_text, count=1)
                    except re.error: # Handle potential regex errors from complex phone numbers
                        working_info_text = working_info_text.replace(phone_match.group(0), "") # Fallback
                   
                # Clean working text after removals
                working_info_text = clean_text(working_info_text.replace('· ·', '·').strip(' ·'))
                print(f"  Text for Type/Address: '{working_info_text}'")

                # 5. Extract Type (Keywords first)
                known_types = ["Restaurant", "Cafe", "Bar", "Pub", "Hotel", "Steakhouse", "Steak", "Chophouse", "Pakistani", "Indian", "Chinese", "Italian", "Thai", "Japanese", "Mexican", "Greek", "Turkish", "Lebanese", "Brunch", "Bakery", "Dessert", "Coffee", "Tea", "Fast Food", "Fine Dining", "Barbecue"]
                type_found = False
                for k_type in known_types:
                    # Use word boundaries for safer matching
                    # Moved try-except inside the loop to handle individual regex errors gracefully
                    try:
                        type_match_obj = re.search(r'\b{}\b'.format(re.escape(k_type)), working_info_text, re.IGNORECASE)
                        if type_match_obj:
                            place_type = k_type # Use the canonical form
                            print(f"    Type Found (Keyword): '{place_type}', Match: '{type_match_obj.group(0)}'")
                            working_info_text = remove_match_from_string(working_info_text, type_match_obj)
                            type_found = True
                            break
                    except re.error: # Skip type if regex fails
                        print(f"    Regex error matching type: {k_type}")
                        continue # Continue to the next type keyword
                       
                if not type_found:
                    print(f"    Type not found via keywords.")
                    # Consider removing fallback logic entirely if it wasn't reliable

                # Clean text again before address extraction
                working_info_text = clean_text(working_info_text.strip(' ·'))
                print(f"  Text for Address: '{working_info_text}'")

                # 6. Extract Address (Remaining text after cleaning hours from the end)
                if working_info_text:
                    address = re.sub(r'(\s*·\s*)?(Open|Closed|Closes|Opening|Hours|Serves|Delivers|Takeout|Dine-in|Pickup|Delivery|Offers|Ends|Starts|Temporary|Permanently).*$', '', working_info_text, flags=re.IGNORECASE).strip()
                    address = clean_text(address)
                    if not address or len(address) < 5: 
                        address = "N/A" 
               
                # Final sanity checks
                if place_type == address : place_type = "N/A" # If type ended up same as address, clear type
                if not place_type or place_type.isdigit(): place_type = "N/A"
                if not address or address.isdigit(): address = "N/A"

            except Exception as e:
                print(f"  Error during detailed info extraction: {e}")
                # Ensure all fields have a default
                name, rating, reviews, price_str, place_type, address, phone_number = \
                    map(lambda x: x if x != "N/A" else "N/A", \
                        [name, rating, reviews, price_str, place_type, address, phone_number])

            result_data = {
                "Name": name, # Capitalized keys for output
                "Rating": rating,
                "Reviews": reviews,
                "Price": price_str,
                "Type": place_type,
                "Address": address,
                "Phone Number": phone_number
            }
            results.append(result_data)
            print(f"  -> Appended: {result_data}")

        print(f"\nExtraction complete. Appended data for {len(results)} places.")
        await browser.close()
        return results

async def main():
    # --- Argument Parsing --- ADDED
    parser = argparse.ArgumentParser(description="Scrape Google Maps for a search query.")
    parser.add_argument("search_query", help="The search query (e.g., 'restaurants in London', 'cafes in Paris')")
    args = parser.parse_args()

    # --- User Input ---
    # search_query = "restaurants in London" # REMOVED: Hardcoded query
    search_query = args.search_query # Use the argument
    print(f"Starting scraper for: {search_query}")

    scraped_data = await scrape_google_maps(search_query)

    if scraped_data:
        print("\n--- Extracted Data ---")
        # Import pandas just for display, if available. Otherwise print manually.
        try:
            import pandas as pd
            # Ensure consistent column order
            df = pd.DataFrame(scraped_data, columns=["Name", "Rating", "Reviews", "Price", "Type", "Address", "Phone Number"])
            print(df.to_string())
        except ImportError:
            print("Pandas not installed. Printing manually:")
            for i, place in enumerate(scraped_data):
                 print(f"\n--- Result {i+1} ---")
                 # Print in specific order
                 for key in ["Name", "Rating", "Reviews", "Price", "Type", "Address", "Phone Number"]:
                     value = place.get(key, "N/A") # Use .get for safety
                     print(f"  {key}: {value}")
        print(f"\n----------------------\nTotal: {len(scraped_data)} places")
    else:
        print("No data was extracted or appended.")

if __name__ == "__main__":
    asyncio.run(main()) 