# Chicago Grocery Walkability Map

An interactive map showing grocery store accessibility within a 12-minute walk (approximately 1 km) from various locations in Chicago.

## Project Structure

```
chicago-grocery-walkability/
├─ build_map.py              # Main script to build the map
├─ requirements.txt          # Python dependencies
├─ README.md                 # This file
├─ .gitignore                # Git ignore rules
├─ docs/
│  └─ index.html             # Generated interactive map
└─ .github/
   └─ workflows/
      └─ build-map.yml       # GitHub Actions CI/CD workflow
```

## Installation

1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd chicago-grocery-walkability
   ```

2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

Build the grocery walkability map:

```bash
python build_map.py
```

This will generate an interactive map at `docs/index.html`.

## Requirements

- Python 3.8+
- See `requirements.txt` for Python package dependencies

## Features

- Interactive map visualization using Folium
- 12-minute walkability analysis to grocery stores
- Chicago neighborhood and area coverage

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]  
