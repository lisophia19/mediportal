# ZIP centroid lookup (spec §8.3) -- straight-line haversine proximity
# ranking, no external geocoding API. Covers the seeded practice ZIPs
# (label = practice name) plus a spread of other Long Island ZIPs so
# proximity-ranking demos have somewhere realistic to call from.
#
# Coordinates are approximate town/ZIP centroids, not surveyed -- adequate
# for a haversine-miles demo, not for anything precision-sensitive.
ZIP_CENTROIDS = {
    # Real LIBJ practice locations (spec §8.1/§8.3).
    "11968": (40.8676, -72.3882, "Southampton"),
    "11901": (40.9176, -72.6620, "Riverhead"),
    "11787": (40.8556, -73.2029, "Smithtown"),
    "11777": (40.9462, -73.0704, "Port Jefferson"),
    "11747": (40.7940, -73.4154, "Melville"),
    # Real Orlin & Cohen practice locations 
    "11746": (40.8698, -73.4001, "Huntington Station"),
    "11570": (40.6576, -73.6412, "Rockville Centre"),
    "11797": (40.8151, -73.4682, "Woodbury"),
    # Spread of other Long Island caller ZIPs for proximity demos.
    "11563": (40.6551, -73.6768, "Lynbrook"),
    "11566": (40.6668, -73.5502, "Merrick"),
    "11758": (40.6784, -73.4737, "Massapequa"),
    "11743": (40.8682, -73.4257, "Huntington"),
    "11772": (40.7659, -73.0148, "Patchogue"),
    "11706": (40.7237, -73.2454, "Bay Shore"),
    "11801": (40.7684, -73.5251, "Hicksville"),
    "11530": (40.7268, -73.6343, "Garden City"),
    "11725": (40.8429, -73.2929, "Commack"),
    "11751": (40.7301, -73.2098, "Islip"),
    "11716": (40.7801, -73.1140, "Bohemia"),
    "11952": (40.9979, -72.5432, "Mattituck"),
    "11954": (41.0362, -71.9545, "Montauk"),
}
