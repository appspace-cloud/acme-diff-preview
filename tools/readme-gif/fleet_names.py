"""Demo environment names for the README GIFs.

They follow the production convention exactly — `pv-<customer>-<cell>`, cells
a/b/c, as used across acme-config-* — so the GIF reads like a real fleet.

Every name here was screened against the 293 distinct customer short-names in
acme-config-{dev,stage,prod}: NONE of them is an actual Appspace customer.
That is deliberate. The README is a shareable artifact and the second GIF
invents a near-miss outage; neither should name a real customer.
"""

# 24 environments taking the identical monthly transition.
FLEET = [
    "pv-adidas-a", "pv-ikea-b", "pv-siemens-a", "pv-bosch-c",
    "pv-netflix-a", "pv-lego-b", "pv-michelin-a", "pv-decathlon-c",
    "pv-nintendo-a", "pv-yamaha-b", "pv-kellogg-a", "pv-lufthansa-c",
    "pv-volvo-a", "pv-mazda-b", "pv-subaru-a", "pv-panasonic-c",
    "pv-philips-a", "pv-electrolux-b", "pv-nestle-a", "pv-ryanair-c",
    "pv-iberia-a", "pv-zara-b", "pv-repsol-a", "pv-heineken-b",
]

# The one that is not like the others (must be a member of FLEET).
NEEDLE_ENV = "pv-heineken-b"

# Environments the bump genuinely does not touch.
UNTOUCHED = ["pv-telefonica-c", "pv-mapfre-a"]

# The blocked-decommission scenario.
DANGER = "pv-michelin-a"
DANGER_CUSTOMER = "michelin"
