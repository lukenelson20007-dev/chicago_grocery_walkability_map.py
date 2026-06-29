import os
import json
import time
import math
import html
import requests
from pathlib import Path


# ============================================================
# USER SETTINGS
# ============================================================

ORS_API_KEY = os.getenv("ORS_API_KEY", "").strip()

OUTPUT_DIR = Path("docs")
OUTPUT_HTML = OUTPUT_DIR / "index.html"
STORE_INVENTORY_JSON = Path("chicago_grocery_store_inventory.json")

# Chicago approximate bounding box:
# south, west, north, east
CHICAGO_BBOX = {
    "south": 41.6445,
    "west": -87.9401,
    "north": 42.0230,
    "east": -87.5241
}

# 12 minutes in seconds
WALK_TIME_SECONDS = 12 * 60

# Testing mode. Set to None for all stores.
MAX_STORES_FOR_TESTING = 75

# If True, includes corner stores / convenience stores.
# I recommend False first for cleaner grocery access analysis.
INCLUDE_CONVENIENCE = False

SLEEP_BETWEEN_ISOCHRONE_CALLS = 0.4

MAP_CENTER = [41.8781, -87.6298]
MAP_START_ZOOM = 11


# ============================================================
# HELPERS
# ============================================================

def require_api_key():
    if not ORS_API_KEY:
        raise ValueError(
            "Missing ORS_API_KEY. Add it as a GitHub Codespaces secret, "
            "GitHub Actions secret, or export it in your shell before running."
        )


def clean_name(name):
    if not name:
        return "Unnamed grocery store"
    return " ".join(str(name).strip().split())


def safe_js_string(value):
    return json.dumps(value, ensure_ascii=False)


def haversine_distance_meters(lat1, lon1, lat2, lon2):
    radius_earth_m = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_earth_m * c


def build_overpass_query():
    s = CHICAGO_BBOX["south"]
    w = CHICAGO_BBOX["west"]
    n = CHICAGO_BBOX["north"]
    e = CHICAGO_BBOX["east"]

    filters = [
        '["shop"="supermarket"]',
        '["shop"="grocery"]',
        '["shop"="greengrocer"]',
        '["shop"="health_food"]',
        '["amenity"="marketplace"]'
    ]

    if INCLUDE_CONVENIENCE:
        filters.append('["shop"="convenience"]')

    query_parts = []

    for filter_text in filters:
        query_parts.append(f"node{filter_text}({s},{w},{n},{e});")
        query_parts.append(f"way{filter_text}({s},{w},{n},{e});")
        query_parts.append(f"relation{filter_text}({s},{w},{n},{e});")

    return f"""
    [out:json][timeout:180];
    (
      {' '.join(query_parts)}
    );
    out center tags;
    """


def fetch_grocery_stores_from_osm():
    print("Fetching grocery stores from OpenStreetMap via Overpass...")

    overpass_url = "https://overpass-api.de/api/interpreter"
    overpass_query = build_overpass_query()

    response = requests.post(
        overpass_url,
        data={"data": overpass_query},
        timeout=240
    )
    response.raise_for_status()

    data = response.json()
    raw_stores = []

    for element in data.get("elements", []):
        tags = element.get("tags", {})

        if element.get("type") == "node":
            lat = element.get("lat")
            lon = element.get("lon")
        else:
            center = element.get("center", {})
            lat = center.get("lat")
            lon = center.get("lon")

        if lat is None or lon is None:
            continue

        name = clean_name(tags.get("name"))
        shop_type = tags.get("shop", "")
        amenity = tags.get("amenity", "")

        raw_stores.append({
            "name": name,
            "lat": float(lat),
            "lon": float(lon),
            "shop_type": shop_type,
            "amenity": amenity,
            "osm_type": element.get("type"),
            "osm_id": element.get("id"),
            "source": "OpenStreetMap"
        })

    print(f"Raw grocery-like records found: {len(raw_stores)}")

    stores = dedupe_stores(raw_stores)

    if MAX_STORES_FOR_TESTING is not None:
        print(f"Testing mode enabled. Keeping first {MAX_STORES_FOR_TESTING} stores.")
        stores = stores[:MAX_STORES_FOR_TESTING]

    for i, store in enumerate(stores, start=1):
        store["store_id"] = f"store_{i:04d}"

    print(f"Stores after de-duplication/filtering: {len(stores)}")

    with STORE_INVENTORY_JSON.open("w", encoding="utf-8") as f:
        json.dump(stores, f, indent=2, ensure_ascii=False)

    return stores


def dedupe_stores(stores, distance_threshold_meters=35):
    unique_stores = []

    for store in stores:
        duplicate_found = False

        for existing in unique_stores:
            same_name = store["name"].lower() == existing["name"].lower()
            distance = haversine_distance_meters(
                store["lat"],
                store["lon"],
                existing["lat"],
                existing["lon"]
            )

            if same_name and distance <= distance_threshold_meters:
                duplicate_found = True
                break

        if not duplicate_found:
            unique_stores.append(store)

    unique_stores.sort(key=lambda x: x["name"].lower())
    return unique_stores


def fetch_cta_rail_geojson():
    print("Fetching CTA rail lines from Chicago Data Portal...")

    cta_geojson_url = "https://data.cityofchicago.org/resource/xbyr-jnvx.geojson?$limit=50000"

    try:
        response = requests.get(cta_geojson_url, timeout=120)
        response.raise_for_status()
        geojson = response.json()
        print(f"CTA rail features found: {len(geojson.get('features', []))}")
        return geojson

    except Exception as exc:
        print(f"Could not fetch CTA rail lines. Continuing without them. Error: {exc}")
        return {"type": "FeatureCollection", "features": []}


def get_isochrone_for_store(store):
    url = "https://api.openrouteservice.org/v2/isochrones/foot-walking"

    headers = {
        "Authorization": ORS_API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "locations": [[store["lon"], store["lat"]]],
        "range": [WALK_TIME_SECONDS],
        "range_type": "time",
        "attributes": ["area"]
    }

    response = requests.post(url, headers=headers, json=payload, timeout=120)
    response.raise_for_status()

    geojson = response.json()

    # Attach store_id and name to returned features so the slicer can control them.
    for feature in geojson.get("features", []):
        feature.setdefault("properties", {})
        feature["properties"]["store_id"] = store["store_id"]
        feature["properties"]["store_name"] = store["name"]

    return geojson


def build_store_feature_collection(stores):
    features = []

    for store in stores:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [store["lon"], store["lat"]]
            },
            "properties": {
                "store_id": store["store_id"],
                "name": store["name"],
                "shop_type": store.get("shop_type") or store.get("amenity") or "grocery-related",
                "osm_type": store.get("osm_type"),
                "osm_id": store.get("osm_id"),
                "lat": store["lat"],
                "lon": store["lon"]
            }
        })

    return {
        "type": "FeatureCollection",
        "features": features
    }


def build_isochrone_feature_collection(stores):
    all_features = []
    successful = 0
    failed = 0

    for index, store in enumerate(stores, start=1):
        print(f"Creating isochrone {index}/{len(stores)}: {store['name']}")

        try:
            iso_geojson = get_isochrone_for_store(store)
            all_features.extend(iso_geojson.get("features", []))
            successful += 1
            time.sleep(SLEEP_BETWEEN_ISOCHRONE_CALLS)

        except Exception as exc:
            print(f"  Failed for {store['name']}: {exc}")
            failed += 1

    print(f"Isochrones successful: {successful}")
    print(f"Isochrones failed: {failed}")

    return {
        "type": "FeatureCollection",
        "features": all_features
    }


def generate_html(stores_geojson, isochrones_geojson, rail_geojson):
    stores_json = json.dumps(stores_geojson, ensure_ascii=False)
    isochrones_json = json.dumps(isochrones_geojson, ensure_ascii=False)
    rail_json = json.dumps(rail_geojson, ensure_ascii=False)

    store_list = sorted(
        [
            {
                "store_id": feature["properties"]["store_id"],
                "name": feature["properties"]["name"]
            }
            for feature in stores_geojson["features"]
        ],
        key=lambda x: x["name"].lower()
    )

    store_list_json = json.dumps(store_list, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Chicago Grocery Store Walkability</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">

  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
  />

  <style>
    html, body {{
      height: 100%;
      margin: 0;
      font-family: Arial, sans-serif;
    }}

    #map {{
      height: 100%;
      width: 100%;
    }}

    .title-box {{
      position: absolute;
      top: 12px;
      left: 56px;
      z-index: 1000;
      background: white;
      padding: 10px 14px;
      border: 2px solid #444;
      border-radius: 6px;
      box-shadow: 2px 2px 6px rgba(0,0,0,0.25);
      max-width: 360px;
    }}

    .title-box h1 {{
      font-size: 16px;
      margin: 0 0 4px 0;
    }}

    .title-box div {{
      font-size: 13px;
      color: #333;
    }}

    .slicer-panel {{
      position: absolute;
      top: 86px;
      right: 14px;
      width: 310px;
      max-height: calc(100% - 130px);
      z-index: 1000;
      background: white;
      border: 2px solid #444;
      border-radius: 6px;
      box-shadow: 2px 2px 6px rgba(0,0,0,0.25);
      display: flex;
      flex-direction: column;
    }}

    .slicer-header {{
      padding: 10px 12px;
      border-bottom: 1px solid #ccc;
      background: #f7f7f7;
    }}

    .slicer-header h2 {{
      margin: 0 0 8px 0;
      font-size: 15px;
    }}

    .slicer-actions {{
      display: flex;
      gap: 6px;
      margin-bottom: 8px;
    }}

    .slicer-actions button {{
      font-size: 12px;
      padding: 4px 7px;
      border: 1px solid #999;
      border-radius: 4px;
      background: #f2f2f2;
      cursor: pointer;
    }}

    #storeSearch {{
      width: 100%;
      box-sizing: border-box;
      padding: 6px 8px;
      border: 1px solid #999;
      border-radius: 4px;
      font-size: 13px;
    }}

    .slicer-list {{
      overflow-y: auto;
      padding: 8px 12px 12px 12px;
    }}

    .store-row {{
      display: flex;
      align-items: flex-start;
      gap: 6px;
      margin-bottom: 7px;
      font-size: 13px;
      line-height: 1.25;
    }}

    .store-row input {{
      margin-top: 1px;
    }}

    .legend-box {{
      position: absolute;
      bottom: 35px;
      left: 50px;
      z-index: 1000;
      background: white;
      padding: 10px 14px;
      border: 2px solid #444;
      border-radius: 6px;
      font-size: 14px;
      box-shadow: 2px 2px 6px rgba(0,0,0,0.25);
    }}

    .count-row {{
      margin-top: 7px;
      font-size: 12px;
      color: #444;
    }}

    @media (max-width: 800px) {{
      .slicer-panel {{
        width: 260px;
        right: 8px;
      }}

      .title-box {{
        max-width: 280px;
      }}
    }}
  </style>
</head>

<body>
  <div id="map"></div>

  <div class="title-box">
    <h1>Chicago Grocery Store Walkability</h1>
    <div>12-minute walking areas around selected grocery stores</div>
  </div>

  <div class="slicer-panel">
    <div class="slicer-header">
      <h2>Grocery store slicer</h2>
      <div class="slicer-actions">
        <button id="selectAllBtn">Select all</button>
        <button id="clearAllBtn">Clear all</button>
      </div>
      <input id="storeSearch" type="text" placeholder="Search store names..." />
      <div class="count-row">
        <span id="visibleCount"></span>
      </div>
    </div>
    <div id="storeList" class="slicer-list"></div>
  </div>

  <div class="legend-box">
    <b>Legend</b><br>
    <span style="color:#2ca25f;">■</span> 12-minute grocery walking area<br>
    <span style="color:#5e3c99;">━</span> CTA rail line<br>
    <span style="color:#006d2c;">●</span> Grocery store
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

  <script>
    const storesGeoJson = {stores_json};
    const isochronesGeoJson = {isochrones_json};
    const railGeoJson = {rail_json};
    const storeList = {store_list_json};

    const map = L.map("map", {{
      center: [{MAP_CENTER[0]}, {MAP_CENTER[1]}],
      zoom: {MAP_START_ZOOM}
    }});

    const lightBase = L.tileLayer(
      "https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png",
      {{
        maxZoom: 20,
        attribution: '&copy; OpenStreetMap contributors &copy; CARTO'
      }}
    ).addTo(map);

    const osmBase = L.tileLayer(
      "https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png",
      {{
        maxZoom: 19,
        attribution: '&copy; OpenStreetMap contributors'
      }}
    );

    const darkBase = L.tileLayer(
      "https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png",
      {{
        maxZoom: 20,
        attribution: '&copy; OpenStreetMap contributors &copy; CARTO'
      }}
    );

    const baseMaps = {{
      "Light streets": lightBase,
      "OpenStreetMap streets": osmBase,
      "Dark basemap": darkBase
    }};

    const railLayer = L.geoJSON(railGeoJson, {{
      style: function(feature) {{
        return {{
          color: "#5e3c99",
          weight: 3,
          opacity: 0.85
        }};
      }}
    }}).addTo(map);

    const activeStoreIds = new Set(storeList.map(s => s.store_id));

    let storeMarkerLayer = L.layerGroup().addTo(map);
    let isochroneLayer = L.layerGroup().addTo(map);

    function storePopupHtml(props) {{
      return `
        <b>${{props.name}}</b><br>
        Type: ${{props.shop_type || "grocery-related"}}<br>
        OSM: ${{props.osm_type}} ${{props.osm_id}}<br>
        Lat/Lon: ${{props.lat.toFixed(6)}}, ${{props.lon.toFixed(6)}}
      `;
    }}

    function drawStoreMarkers() {{
      storeMarkerLayer.clearLayers();

      L.geoJSON(storesGeoJson, {{
        filter: function(feature) {{
          return activeStoreIds.has(feature.properties.store_id);
        }},
        pointToLayer: function(feature, latlng) {{
          return L.circleMarker(latlng, {{
            radius: 6,
            fillColor: "#006d2c",
            color: "#ffffff",
            weight: 1.5,
            fillOpacity: 0.95
          }});
        }},
        onEachFeature: function(feature, layer) {{
          layer.bindPopup(storePopupHtml(feature.properties));
          layer.bindTooltip(feature.properties.name);
        }}
      }}).addTo(storeMarkerLayer);
    }}

    function drawIsochrones() {{
      isochroneLayer.clearLayers();

      L.geoJSON(isochronesGeoJson, {{
        filter: function(feature) {{
          return activeStoreIds.has(feature.properties.store_id);
        }},
        style: function(feature) {{
          return {{
            fillColor: "#2ca25f",
            color: "#006d2c",
            weight: 1.25,
            fillOpacity: 0.30,
            opacity: 0.8
          }};
        }},
        onEachFeature: function(feature, layer) {{
          layer.bindTooltip("12-minute walking area: " + feature.properties.store_name);
        }}
      }}).addTo(isochroneLayer);
    }}

    function redrawMapLayers() {{
      drawIsochrones();
      drawStoreMarkers();
      updateVisibleCount();
    }}

    function updateVisibleCount() {{
      document.getElementById("visibleCount").innerText =
        `${{activeStoreIds.size}} of ${{storeList.length}} stores selected`;
    }}

    function buildSlicer() {{
      const container = document.getElementById("storeList");
      container.innerHTML = "";

      storeList.forEach(store => {{
        const row = document.createElement("label");
        row.className = "store-row";
        row.dataset.storeName = store.name.toLowerCase();

        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = true;
        checkbox.dataset.storeId = store.store_id;

        checkbox.addEventListener("change", function() {{
          if (checkbox.checked) {{
            activeStoreIds.add(store.store_id);
          }} else {{
            activeStoreIds.delete(store.store_id);
          }}
          redrawMapLayers();
        }});

        const nameSpan = document.createElement("span");
        nameSpan.textContent = store.name;

        row.appendChild(checkbox);
        row.appendChild(nameSpan);
        container.appendChild(row);
      }});
    }}

    function setAllStores(checked) {{
      const checkboxes = document.querySelectorAll("#storeList input[type='checkbox']");

      activeStoreIds.clear();

      checkboxes.forEach(cb => {{
        cb.checked = checked;
        if (checked) {{
          activeStoreIds.add(cb.dataset.storeId);
        }}
      }});

      redrawMapLayers();
    }}

    function setupSearch() {{
      const searchInput = document.getElementById("storeSearch");

      searchInput.addEventListener("input", function() {{
        const searchText = searchInput.value.trim().toLowerCase();
        const rows = document.querySelectorAll(".store-row");

        rows.forEach(row => {{
          const name = row.dataset.storeName;
          row.style.display = name.includes(searchText) ? "flex" : "none";
        }});
      }});
    }}

    document.getElementById("selectAllBtn").addEventListener("click", function() {{
      setAllStores(true);
    }});

    document.getElementById("clearAllBtn").addEventListener("click", function() {{
      setAllStores(false);
    }});

    const overlays = {{
      "CTA rail lines": railLayer,
      "Selected grocery stores": storeMarkerLayer,
      "12-minute walkability": isochroneLayer
    }};

    L.control.layers(baseMaps, overlays, {{ collapsed: false }}).addTo(map);
    L.control.scale().addTo(map);

    buildSlicer();
    setupSearch();
    redrawMapLayers();
  </script>
</body>
</html>
"""


def create_map():
    require_api_key()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    stores = fetch_grocery_stores_from_osm()
    stores_geojson = build_store_feature_collection(stores)

    rail_geojson = fetch_cta_rail_geojson()
    isochrones_geojson = build_isochrone_feature_collection(stores)

    html_text = generate_html(
        stores_geojson=stores_geojson,
        isochrones_geojson=isochrones_geojson,
        rail_geojson=rail_geojson
    )

    OUTPUT_HTML.write_text(html_text, encoding="utf-8")

    print("")
    print("Done.")
    print(f"Generated: {OUTPUT_HTML}")
    print("Commit docs/index.html and publish the docs folder with GitHub Pages.")


if __name__ == "__main__":
    create_map()
