"""
metro_areas.py — curated "nearby cities" lists for major US metro areas.

Not real geocoding: there's no offline US-cities/lat-lon database wired into
this app, and adding a live geocoding API call on every resume upload is a
new external dependency for a "nice to have" feature. Instead, each major
metro's home city is hand-mapped to the suburbs/nearby cities a job seeker
living there would realistically also want to see roles in — a rough stand-in
for "within ~50 miles", same curated-not-computed tradeoff already made in
location_groups.py (remote-region classification) and role_synonyms.py
(title synonym expansion).

Coverage is the ~40 largest US metros. A resume listing a smaller city just
falls back to searching that literal "City, ST" string on its own (handled
in resume_parser.py), which is still strictly better than nothing.
"""

METRO_AREAS = {
    ("new york", "ny"): ["New York, NY", "Brooklyn, NY", "Queens, NY", "Jersey City, NJ", "Newark, NJ", "Hoboken, NJ", "Yonkers, NY", "White Plains, NY"],
    ("brooklyn", "ny"): ["New York, NY", "Brooklyn, NY", "Queens, NY", "Jersey City, NJ"],
    ("los angeles", "ca"): ["Los Angeles, CA", "Santa Monica, CA", "Pasadena, CA", "Burbank, CA", "Glendale, CA", "Long Beach, CA", "Anaheim, CA", "Irvine, CA"],
    ("chicago", "il"): ["Chicago, IL", "Evanston, IL", "Oak Park, IL", "Naperville, IL", "Schaumburg, IL", "Cicero, IL"],
    ("houston", "tx"): ["Houston, TX", "Sugar Land, TX", "The Woodlands, TX", "Pasadena, TX", "Katy, TX"],
    ("phoenix", "az"): ["Phoenix, AZ", "Scottsdale, AZ", "Tempe, AZ", "Mesa, AZ", "Chandler, AZ", "Gilbert, AZ", "Glendale, AZ", "Peoria, AZ"],
    ("philadelphia", "pa"): ["Philadelphia, PA", "Camden, NJ", "Cherry Hill, NJ", "King of Prussia, PA", "Wilmington, DE"],
    ("san antonio", "tx"): ["San Antonio, TX", "New Braunfels, TX", "Schertz, TX"],
    ("san diego", "ca"): ["San Diego, CA", "Chula Vista, CA", "Carlsbad, CA", "Encinitas, CA", "La Jolla, CA"],
    ("dallas", "tx"): ["Dallas, TX", "Fort Worth, TX", "Plano, TX", "Irving, TX", "Arlington, TX", "Frisco, TX", "McKinney, TX"],
    ("fort worth", "tx"): ["Dallas, TX", "Fort Worth, TX", "Arlington, TX", "Irving, TX"],
    ("austin", "tx"): ["Austin, TX", "Round Rock, TX", "Cedar Park, TX", "Georgetown, TX", "Pflugerville, TX"],
    ("san jose", "ca"): ["San Jose, CA", "Sunnyvale, CA", "Santa Clara, CA", "Mountain View, CA", "Cupertino, CA", "Palo Alto, CA"],
    ("san francisco", "ca"): ["San Francisco, CA", "Oakland, CA", "Berkeley, CA", "San Mateo, CA", "Daly City, CA", "South San Francisco, CA"],
    ("oakland", "ca"): ["San Francisco, CA", "Oakland, CA", "Berkeley, CA", "Alameda, CA"],
    ("columbus", "oh"): ["Columbus, OH", "Dublin, OH", "Westerville, OH", "Grove City, OH"],
    ("charlotte", "nc"): ["Charlotte, NC", "Concord, NC", "Gastonia, NC", "Huntersville, NC"],
    ("indianapolis", "in"): ["Indianapolis, IN", "Carmel, IN", "Fishers, IN", "Noblesville, IN"],
    ("seattle", "wa"): ["Seattle, WA", "Bellevue, WA", "Redmond, WA", "Kirkland, WA", "Tacoma, WA", "Everett, WA"],
    ("denver", "co"): ["Denver, CO", "Aurora, CO", "Lakewood, CO", "Boulder, CO", "Centennial, CO", "Littleton, CO"],
    ("boston", "ma"): ["Boston, MA", "Cambridge, MA", "Somerville, MA", "Quincy, MA", "Newton, MA", "Waltham, MA"],
    ("cambridge", "ma"): ["Boston, MA", "Cambridge, MA", "Somerville, MA"],
    ("nashville", "tn"): ["Nashville, TN", "Franklin, TN", "Brentwood, TN", "Murfreesboro, TN"],
    ("detroit", "mi"): ["Detroit, MI", "Dearborn, MI", "Ann Arbor, MI", "Warren, MI", "Troy, MI"],
    ("portland", "or"): ["Portland, OR", "Beaverton, OR", "Hillsboro, OR", "Gresham, OR", "Vancouver, WA"],
    ("memphis", "tn"): ["Memphis, TN", "Germantown, TN", "Bartlett, TN"],
    ("oklahoma city", "ok"): ["Oklahoma City, OK", "Edmond, OK", "Norman, OK"],
    ("las vegas", "nv"): ["Las Vegas, NV", "Henderson, NV", "North Las Vegas, NV"],
    ("louisville", "ky"): ["Louisville, KY", "Jeffersonville, IN"],
    ("baltimore", "md"): ["Baltimore, MD", "Towson, MD", "Columbia, MD", "Annapolis, MD"],
    ("milwaukee", "wi"): ["Milwaukee, WI", "Wauwatosa, WI", "Waukesha, WI"],
    ("albuquerque", "nm"): ["Albuquerque, NM", "Rio Rancho, NM", "Santa Fe, NM"],
    ("tucson", "az"): ["Tucson, AZ", "Oro Valley, AZ"],
    ("fresno", "ca"): ["Fresno, CA", "Clovis, CA"],
    ("sacramento", "ca"): ["Sacramento, CA", "Roseville, CA", "Elk Grove, CA", "Folsom, CA"],
    ("mesa", "az"): ["Phoenix, AZ", "Mesa, AZ", "Chandler, AZ", "Gilbert, AZ", "Tempe, AZ"],
    ("atlanta", "ga"): ["Atlanta, GA", "Sandy Springs, GA", "Marietta, GA", "Alpharetta, GA", "Decatur, GA", "Roswell, GA"],
    ("kansas city", "mo"): ["Kansas City, MO", "Overland Park, KS", "Independence, MO", "Olathe, KS"],
    ("colorado springs", "co"): ["Colorado Springs, CO", "Denver, CO"],
    ("omaha", "ne"): ["Omaha, NE", "Council Bluffs, IA", "Bellevue, NE"],
    ("raleigh", "nc"): ["Raleigh, NC", "Durham, NC", "Cary, NC", "Chapel Hill, NC"],
    ("durham", "nc"): ["Raleigh, NC", "Durham, NC", "Chapel Hill, NC", "Cary, NC"],
    ("miami", "fl"): ["Miami, FL", "Fort Lauderdale, FL", "Hialeah, FL", "Coral Gables, FL", "Boca Raton, FL"],
    ("fort lauderdale", "fl"): ["Miami, FL", "Fort Lauderdale, FL", "Boca Raton, FL", "Pompano Beach, FL"],
    ("virginia beach", "va"): ["Virginia Beach, VA", "Norfolk, VA", "Chesapeake, VA"],
    ("minneapolis", "mn"): ["Minneapolis, MN", "Saint Paul, MN", "Bloomington, MN", "Eden Prairie, MN", "Edina, MN"],
    ("saint paul", "mn"): ["Minneapolis, MN", "Saint Paul, MN", "Bloomington, MN"],
    ("tulsa", "ok"): ["Tulsa, OK", "Broken Arrow, OK"],
    ("tampa", "fl"): ["Tampa, FL", "St. Petersburg, FL", "Clearwater, FL", "Brandon, FL"],
    ("st. petersburg", "fl"): ["Tampa, FL", "St. Petersburg, FL", "Clearwater, FL"],
    ("arlington", "tx"): ["Dallas, TX", "Fort Worth, TX", "Arlington, TX", "Irving, TX"],
    ("new orleans", "la"): ["New Orleans, LA", "Metairie, LA", "Kenner, LA"],
    ("wichita", "ks"): ["Wichita, KS"],
    ("cleveland", "oh"): ["Cleveland, OH", "Lakewood, OH", "Parma, OH", "Akron, OH"],
    ("bakersfield", "ca"): ["Bakersfield, CA"],
    ("aurora", "co"): ["Denver, CO", "Aurora, CO", "Centennial, CO"],
    ("anaheim", "ca"): ["Los Angeles, CA", "Anaheim, CA", "Irvine, CA", "Santa Ana, CA"],
    ("santa ana", "ca"): ["Los Angeles, CA", "Irvine, CA", "Santa Ana, CA", "Anaheim, CA"],
    ("st. louis", "mo"): ["St. Louis, MO", "Clayton, MO", "Florissant, MO"],
    ("saint louis", "mo"): ["St. Louis, MO", "Clayton, MO", "Florissant, MO"],
    ("pittsburgh", "pa"): ["Pittsburgh, PA", "Cranberry Township, PA"],
    ("cincinnati", "oh"): ["Cincinnati, OH", "Covington, KY", "Mason, OH"],
    ("orlando", "fl"): ["Orlando, FL", "Winter Park, FL", "Kissimmee, FL", "Lake Mary, FL"],
    ("salt lake city", "ut"): ["Salt Lake City, UT", "Sandy, UT", "West Jordan, UT", "Provo, UT", "Lehi, UT"],
    ("provo", "ut"): ["Salt Lake City, UT", "Provo, UT", "Lehi, UT", "Orem, UT"],
    ("richmond", "va"): ["Richmond, VA", "Henrico, VA", "Chesterfield, VA"],
    ("jacksonville", "fl"): ["Jacksonville, FL", "St. Augustine, FL"],
    ("boise", "id"): ["Boise, ID", "Meridian, ID", "Nampa, ID"],
    ("des moines", "ia"): ["Des Moines, IA", "West Des Moines, IA", "Ankeny, IA"],
    ("washington", "dc"): ["Washington, DC", "Arlington, VA", "Alexandria, VA", "Bethesda, MD", "Silver Spring, MD", "Rockville, MD", "Tysons, VA"],
}


def find_metro_area(city, state):
    """Look up nearby-city list for a "City, ST" pair (case-insensitive).
    Returns None if the city isn't in the curated dataset."""
    if not city or not state:
        return None
    return METRO_AREAS.get((city.strip().lower(), state.strip().lower()))
