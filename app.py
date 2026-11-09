# Import necessary tools for web server, database, url parsing, and file handling
import os
import json
import urllib.parse as urlparse
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_bcrypt import Bcrypt
from google import genai
from google.genai import types
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from html2image import Html2Image

# Start the Flask web application
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "sa_environmental_gis_secure_key")
CORS(app)

# Setup password security and login session tools
bcrypt = Bcrypt(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Pre-registered environmental practitioner account database
USERS_DB = {
    "practitioner@enviro.co.za": {
        "id": 1,
        "email": "practitioner@enviro.co.za",
        "password": bcrypt.generate_password_hash("securepass123").decode('utf-8'),
        "name": "Ntsako Banyini"
    }
}

class User(UserMixin):
    def __init__(self, id, email, name):
        self.id = id
        self.email = email
        self.name = name

@login_manager.user_loader
def load_user(user_id):
    """Check who is currently logged into the system session."""
    for email, data in USERS_DB.items():
        if str(data['id']) == str(user_id):
            return User(data['id'], data['email'], data['name'])
    return None

# Database Connection Setup:
# Automatically detects Render's cloud DATABASE_URL if live, otherwise falls back to local PostgreSQL.
urlparse.uses_netloc.append("postgres")
url = urlparse.urlparse(os.environ.get("DATABASE_URL", ""))

if url.path:
    DB_CONFIG = {
        "dbname": url.path[1:],
        "user": url.username,
        "password": url.password,
        "host": url.hostname,
        "port": url.port or 5432
    }
else:
    DB_CONFIG = {
        "dbname": "sa_gis_db",
        "user": os.environ.get("USER"),
        "password": "",
        "host": "localhost",
        "port": "5432"
    }

def get_db_connection():
    """Open a connection to the PostGIS database using configured settings."""
    return psycopg2.connect(**DB_CONFIG)

# Initialize Google GenAI client for smart location search
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Handle practitioner login page and credential verification."""
    if request.method == 'POST':
        data = request.get_json() if request.is_json else request.form
        email = data.get('email')
        password = data.get('password')

        user_data = USERS_DB.get(email)
        if user_data and bcrypt.check_password_hash(user_data['password'], password):
            user = User(user_data['id'], user_data['email'], user_data['name'])
            login_user(user)
            if request.is_json:
                return jsonify({"status": "success", "message": "Logged in successfully."})
            return redirect(url_for('index'))
        
        if request.is_json:
            return jsonify({"error": "Invalid credentials."}), 401
        return "Invalid credentials", 401

    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    """Log out the active user session and redirect to login."""
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    """Load the main map dashboard (requires practitioner login)."""
    return render_template('index.html', user_name=current_user.name)

@app.route('/api/get-spatial-utilities', methods=['GET'])
@login_required
def get_spatial_utilities():
    """Fetch underground utility lines from PostGIS within the current map view bounds."""
    bbox_str = request.args.get('bbox')
    if not bbox_str:
        return jsonify({"error": "Bounding box required."}), 400

    try:
        min_lon, min_lat, max_lon, max_lat = map(float, bbox_str.split(','))
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        query = """
            SELECT name, utility_type, ST_AsGeoJSON(geom) as geometry
            FROM utilities
            WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326);
        """
        cursor.execute(query, (min_lon, min_lat, max_lon, max_lat))
        rows = cursor.fetchall()
        
        features = [{
            "type": "Feature",
            "properties": {"name": row['name'], "type": row['utility_type']},
            "geometry": json.loads(row['geometry'])
        } for row in rows]

        cursor.close()
        conn.close()
        return jsonify({"type": "FeatureCollection", "features": features})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/get-nema-buffers', methods=['GET'])
@login_required
def get_nema_buffers():
    """Fetch NEMA 32m river protection zones from PostGIS within map bounds."""
    bbox_str = request.args.get('bbox')
    if not bbox_str:
        return jsonify({"error": "Bounding box required."}), 400

    try:
        min_lon, min_lat, max_lon, max_lat = map(float, bbox_str.split(','))
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        query = """
            SELECT name, ST_AsGeoJSON(buffer_geom) as geometry
            FROM v_nema_buffers
            WHERE buffer_geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326);
        """
        cursor.execute(query, (min_lon, min_lat, max_lon, max_lat))
        rows = cursor.fetchall()
        
        features = [{
            "type": "Feature",
            "properties": {"name": row['name'], "compliance": "NEMA 32m Statutory Buffer"},
            "geometry": json.loads(row['geometry'])
        } for row in rows]

        cursor.close()
        conn.close()
        return jsonify({"type": "FeatureCollection", "features": features})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/analyze-site', methods=['GET'])
@login_required
def analyze_site():
    """Run real database checks to summarize environmental profile for the active site view."""
    bbox_str = request.args.get('bbox')
    location_name = request.args.get('location', 'Selected Area')
    
    if not bbox_str:
        return jsonify({"error": "Bounding box required."}), 400

    try:
        min_lon, min_lat, max_lon, max_lat = map(float, bbox_str.split(','))
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute("SELECT name, utility_type FROM utilities WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326);", (min_lon, min_lat, max_lon, max_lat))
        utilities_found = cursor.fetchall()

        cursor.execute("SELECT name FROM v_nema_buffers WHERE buffer_geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326);", (min_lon, min_lat, max_lon, max_lat))
        buffers_found = cursor.fetchall()

        cursor.close()
        conn.close()

        util_descriptions = [f"{u['name']} ({u['utility_type']})" for u in utilities_found]
        buffer_descriptions = [b['name'] for b in buffers_found]

        underground_state = (
            f"Database confirms {len(utilities_found)} active underground line(s): " + 
            ", ".join(util_descriptions) + "." if utilities_found else 
            "No registered underground utility lines crossing this exact map view."
        )

        surface_state = (
            f"Hydrological screening identifies NEMA 32m river protection zones: " + 
            ", ".join(buffer_descriptions) + "." if buffers_found else 
            "No overlapping NEMA 32m river protection corridors in this view."
        )

        return jsonify({
            "location": location_name,
            "utilities_count": len(utilities_found),
            "buffers_count": len(buffers_found),
            "underground_state": underground_state,
            "surface_state": surface_state
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/compliance-matrix', methods=['GET'])
@login_required
def compliance_matrix():
    """Execute PostGIS spatial compliance audit rules function."""
    bbox_str = request.args.get('bbox')
    if not bbox_str:
        return jsonify({"error": "Bounding box required."}), 400

    try:
        min_lon, min_lat, max_lon, max_lat = map(float, bbox_str.split(','))
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM fn_screen_site_compliance(%s, %s, %s, %s);", (min_lon, min_lat, max_lon, max_lat))
        scorecard = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify({"status": "success", "scorecard": scorecard})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/export-kml', methods=['GET'])
@login_required
def export_kml():
    """Export boundary as a downloadable KML file for Google Earth."""
    location = request.args.get('location', 'Site_Footprint')
    bbox_str = request.args.get('bbox')
    if not bbox_str:
        return jsonify({"error": "Bounding box required."}), 400

    min_lon, min_lat, max_lon, max_lat = map(float, bbox_str.split(','))
    kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{location} Spatial Boundary</name>
    <Placemark>
      <name>{location} Target Envelope</name>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              {min_lon},{min_lat},0
              {max_lon},{min_lat},0
              {max_lon},{max_lat},0
              {min_lon},{max_lat},0
              {min_lon},{min_lat},0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>"""

    path = "output_site_boundary.kml"
    with open(path, "w") as f:
        f.write(kml_content)
    return send_file(path, as_attachment=True, download_name=f"{location}_boundary.kml")

@app.route('/api/export-geojson', methods=['GET'])
@login_required
def export_geojson():
    """Export boundary as GeoJSON for web editors and GIS software."""
    location = request.args.get('location', 'Site_Footprint')
    bbox_str = request.args.get('bbox')
    if not bbox_str:
        return jsonify({"error": "Bounding box required."}), 400

    min_lon, min_lat, max_lon, max_lat = map(float, bbox_str.split(','))
    geojson_data = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"name": f"{location} Site Boundary"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[min_lon, min_lat], [max_lon, min_lat], [max_lon, max_lat], [min_lon, max_lat], [min_lon, min_lat]]]
            }
        }]
    }

    path = "output_site_boundary.geojson"
    with open(path, "w") as f:
        json.dump(geojson_data, f, indent=2)

    return send_file(path, as_attachment=True, download_name=f"{location}_boundary.geojson")

@app.route('/api/export-csv-metrics', methods=['GET'])
@login_required
def export_csv_metrics():
    """Export infrastructure and buffer metrics as a spreadsheet CSV file."""
    location = request.args.get('location', 'Site_Assessment')
    bbox_str = request.args.get('bbox')
    if not bbox_str:
        return jsonify({"error": "Bounding box required."}), 400

    min_lon, min_lat, max_lon, max_lat = map(float, bbox_str.split(','))
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT name, utility_type FROM utilities WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326);", (min_lon, min_lat, max_lon, max_lat))
        utils = cursor.fetchall()
        
        cursor.execute("SELECT name FROM v_nema_buffers WHERE buffer_geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326);", (min_lon, min_lat, max_lon, max_lat))
        buffers = cursor.fetchall()
        cursor.close()
        conn.close()

        csv_path = "eia_infrastructure_metrics.csv"
        with open(csv_path, "w") as f:
            f.write("Category,Record Name,Status,Notes\n")
            for u in utils:
                f.write(f"Underground Utility,{u['name']},WARNING,Type: {u['utility_type']}\n")
            for b in buffers:
                f.write(f"NEMA 32m Buffer,{b['name']},ACTION REQUIRED,River protection zone\n")

        return send_file(csv_path, as_attachment=True, download_name=f"{location}_Infrastructure_Metrics.csv")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/iaap-register', methods=['POST'])
@login_required
def register_iaap():
    """Save stakeholder public participation comments into database table."""
    data = request.get_json()
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO iaap_registry (project_name, party_name, organization, email, comment) VALUES (%s, %s, %s, %s, %s);",
            (data.get('project_name', 'General Assessment'), data['party_name'], data.get('organization', 'Public'), data.get('email', ''), data.get('comment', ''))
        )
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"status": "success", "message": "Registered."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/parse-prompt', methods=['POST'])
@login_required
def parse_prompt():
    """Use Google Gemini AI to extract South African place names from text prompts."""
    req_data = request.get_json()
    user_text = req_data.get('prompt', '')
    system_instruction = "Extract South African place name. Return JSON: {\"location\": \"string\"}"
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_text,
            config=types.GenerateContentConfig(system_instruction=system_instruction, response_mime_type="application/json")
        )
        return response.text
    except Exception as e:
        return jsonify({"error": "AI failed."}), 500

@app.route('/api/export-pdf', methods=['GET', 'POST'])
@login_required
def generate_pdf():
    """Take a headless screenshot of the map and build an official PDF compliance snapshot."""
    project_name = request.args.get('project', 'Site Assessment')
    practitioner = request.args.get('practitioner', current_user.name)
    location = request.args.get('location', 'South Africa')
    
    pdf_path = "output_eic_map.pdf"
    snapshot_path = "map_snapshot.png"

    try:
        hti = Html2Image(output_path='.')
        hti.screenshot(url='http://127.0.0.1:5000/', save_as=snapshot_path, size=(1200, 800))
    except Exception as e:
        print(f"Snapshot warning: {e}")

    c = canvas.Canvas(pdf_path, pagesize=landscape(A4))
    width, height = landscape(A4)
    c.setLineWidth(2)
    c.rect(20, 20, width - 40, height - 40)

    if os.path.exists(snapshot_path):
        c.drawImage(snapshot_path, 30, 150, width=width - 60, height=height - 170, preserveAspectRatio=True)

    c.setFillColor(colors.white)
    c.rect(width - 260, 30, 230, 110, fill=1, stroke=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(width - 250, 120, "ENVIRONMENTAL IMPACT ASSESSMENT")
    c.setFont("Helvetica", 9)
    c.drawString(width - 250, 102, f"Project: {project_name[:35]}")
    c.drawString(width - 250, 88, f"Location: {location[:35]}")
    c.drawString(width - 250, 74, f"Officer: {practitioner[:35]}")

    c.save()
    return send_file(pdf_path, as_attachment=True, download_name="EIA_Compliance_Map.pdf")

if __name__ == '__main__':
    app.run(debug=True, port=5000)