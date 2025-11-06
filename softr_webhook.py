# app.py
"""
Invoice Extractor API for HTML UI & Softr Integration – PDF & image support.
This API receives invoice uploads (file or URL) and extracts data to Airtable.
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import base64
import json
from openai import OpenAI
from pyairtable import Api
import tempfile
import threading
import requests
from datetime import datetime

app = Flask(__name__)
CORS(app)

# --- Configuration via environment variables ---
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
AIRTABLE_API_KEY = os.getenv('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID')
AIRTABLE_TABLE_NAME = os.getenv('AIRTABLE_TABLE_NAME', 'Invoices')

if not OPENAI_API_KEY or not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
    raise Exception("Missing required environment variables: OPENAI_API_KEY, AIRTABLE_API_KEY, AIRTABLE_BASE_ID")

# Initialize clients
openai_client = OpenAI(api_key=OPENAI_API_KEY)
airtable_api = Api(AIRTABLE_API_KEY)
airtable_table = airtable_api.table(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME)

def pdf_to_image(pdf_path):
    """Convert first page of PDF to image (via PyMuPDF)"""
    import fitz  # PyMuPDF
    print(f"📄 Opening PDF: {pdf_path}")
    pdf_doc = fitz.open(pdf_path)
    if pdf_doc.page_count == 0:
        raise Exception("PDF has no pages")
    page = pdf_doc[0]
    mat = fitz.Matrix(2,2)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("png")
    img_path = pdf_path.replace('.pdf', '.png')
    with open(img_path, 'wb') as f:
        f.write(img_bytes)
    pdf_doc.close()
    print(f"✅ PDF converted to image: {img_path}")
    return img_path

def extract_invoice_data(file_path):
    """Extract invoice data from image or PDF via OpenAI."""
    # If PDF, convert to image
    if file_path.lower().endswith('.pdf'):
        print("📄 PDF detected, converting to image...")
        file_path = pdf_to_image(file_path)

    # Read file bytes
    print(f"📖 Reading file: {file_path}")
    with open(file_path, "rb") as img_file:
        img_bytes = img_file.read()
        base64_image = base64.b64encode(img_bytes).decode('utf-8')

    file_ext = file_path.lower().split('.')[-1]
    mime_type = {
        'jpg':'image/jpeg', 'jpeg':'image/jpeg',
        'png':'image/png','gif':'image/gif','webp':'image/webp'
    }.get(file_ext, 'image/png')

    print(f"📤 Sending to OpenAI – type: {mime_type}, size: {len(img_bytes)} bytes")

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": """Extract the following information from this invoice and return as JSON:
{
  "invoice_number": "string",
  "invoice_date": "YYYY-MM-DD",
  "vendor_name": "string",
  "vendor_address": "string",
  "customer_name": "string",
  "customer_address": "string",
  "subtotal": number,
  "tax": number,
  "total_amount": number,
  "currency": "string",
  "line_items": [
    {
      "description": "string",
      "quantity": number,
      "unit_price": number,
      "amount": number
    }
  ]
}
Return ONLY valid JSON. Use null for missing fields."""
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{base64_image}"
                        }
                    }
                ]
            }
        ],
        max_tokens=1500
    )

    content = response.choices[0].message.content
    print(f"🤖 OpenAI response length: {len(content)} chars")

    # Remove ```json blocks if present
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        content = content.split("```")[1].split("```")[0]

    data = json.loads(content.strip())
    return data

def save_to_airtable(invoice_data, source_file_url=None):
    """Save extracted data to Airtable."""
    # Prepare line items text
    line_items = invoice_data.get("line_items", [])
    items_text = "\n".join([
        f"{itm.get('description','')} – Qty: {itm.get('quantity',0)} × {itm.get('unit_price',0)} = {itm.get('amount',0)}"
        for itm in line_items
    ])

    record = {
        "Invoice Number": invoice_data.get("invoice_number"),
        "Invoice Date": invoice_data.get("invoice_date"),
        "Vendor Name": invoice_data.get("vendor_name"),
        "Vendor Address": invoice_data.get("vendor_address"),
        "Customer Name": invoice_data.get("customer_name"),
        "Customer Address": invoice_data.get("customer_address"),
        "Subtotal": invoice_data.get("subtotal"),
        "Tax": invoice_data.get("tax"),
        "Total Amount": invoice_data.get("total_amount"),
        "Currency": invoice_data.get("currency"),
        "Line Items": items_text,
        "Status": "Extracted"
    }
    if source_file_url:
        record["Source File URL"] = source_file_url

    # Filter out None values
    record = {k:v for k,v in record.items() if v is not None}
    print(f"💾 Writing to Airtable with {len(record)} fields")
    created = airtable_table.create(record)
    return created

def process_background(file_path=None, file_url=None):
    """Background job: download or use local, extract, save."""
    try:
        tmp_path = None
        source_url = None

        if file_url:
            source_url = file_url
            print(f"⬇️ Downloading invoice from URL: {file_url}")
            r = requests.get(file_url, timeout=30)
            r.raise_for_status()
            ext = 'pdf' if ('pdf' in r.headers.get('content-type','') or file_url.lower().endswith('.pdf')) else 'jpg'
            with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}') as tmp:
                tmp.write(r.content)
                tmp_path = tmp.name
            print(f"💾 Downloaded to temp: {tmp_path}")
        elif file_path:
            tmp_path = file_path
        else:
            print("⚠️ No input file specified.")
            return

        # Extract
        invoice_data = extract_invoice_data(tmp_path)
        print(f"✅ Extracted invoice number: {invoice_data.get('invoice_number')}")

        # Save
        saved = save_to_airtable(invoice_data, source_file_url=source_url)
        print(f"✅ Airtable record ID: {saved.get('id')}")

    except Exception as e:
        print(f"❌ Error during processing: {e}")
    finally:
        # cleanup tmp file
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            png_path = tmp_path.replace('.pdf', '.png') if tmp_path else None
            if png_path and os.path.exists(png_path):
                os.unlink(png_path)
        except Exception as cleanup_err:
            print(f"⚠️ Cleanup error: {cleanup_err}")

@app.route('/')
def home():
    """Home page"""
    return jsonify({
        "status": "active",
        "message": "Invoice Extractor API - HTML UI & Softr",
        "version": "3.0",
        "supported_formats": ["JPG", "JPEG", "PNG", "PDF"],
        "endpoints": {
            "/webhook": "POST - For HTML UI (sync response)",
            "/softr-webhook": "POST - For Softr (async)", 
            "/health": "GET - Health check"
        }
    })

@app.route('/webhook', methods=['POST'])
def webhook():
    """Main endpoint for HTML UI - processes synchronously"""
    tmp_path = None
    png_path = None
    
    try:
        print("\n" + "="*60)
        print("📨 /webhook called from HTML UI")
        
        # Handle file upload
        if 'file' in request.files:
            file = request.files['file']
            
            if file.filename == '':
                return jsonify({"error": "No file selected"}), 400
            
            ext = file.filename.rsplit('.',1)[1].lower() if '.' in file.filename else 'jpg'
            
            # Validate
            allowed = {'pdf','jpg','jpeg','png','gif','webp'}
            if ext not in allowed:
                return jsonify({"error": f"Invalid type. Allowed: {', '.join(allowed)}"}), 400
            
            print(f"📎 File: {file.filename} ({ext})")
            
            # Save temp
            with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}') as tmp:
                file.save(tmp.name)
                tmp_path = tmp.name
            
            # Extract NOW (synchronous)
            print("🤖 Extracting...")
            invoice_data = extract_invoice_data(tmp_path)
            
            # Save NOW
            print("💾 Saving to Airtable...")
            airtable_record = save_to_airtable(invoice_data)
            
            # Cleanup
            try:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                png_path = tmp_path.replace('.pdf', '.png')
                if png_path and os.path.exists(png_path):
                    os.unlink(png_path)
            except:
                pass
            
            print("✅ Done!")
            print("="*60 + "\n")
            
            # Return data to HTML UI
            return jsonify({
                "success": True,
                "message": "Invoice processed successfully",
                "invoice_number": invoice_data.get("invoice_number"),
                "total_amount": invoice_data.get("total_amount"),
                "currency": invoice_data.get("currency"),
                "airtable_record_id": airtable_record['id'],
                "data": invoice_data
            }), 200

        # Handle URL
        elif request.is_json:
            data = request.get_json()
            file_url = data.get('file_url') or data.get('fileUrl')
            
            if not file_url:
                return jsonify({"error": "No file_url"}), 400
            
            # Background for URLs
            threading.Thread(target=process_background, args=(None, file_url)).start()
            
            return jsonify({
                "success": True,
                "message": "Processing..."
            }), 202
        
        else:
            return jsonify({"error": "No file provided"}), 400

    except Exception as e:
        print(f"❌ Error: {e}")
        
        # Cleanup on error
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            png_path = tmp_path.replace('.pdf', '.png') if tmp_path else None
            if png_path and os.path.exists(png_path):
                os.unlink(png_path)
        except:
            pass
        
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/softr-webhook', methods=['POST'])
def softr_webhook():
    """Endpoint for Softr - background processing"""
    try:
        print("\n" + "="*60)
        print("📨 /softr-webhook called")

        if 'file' in request.files:
            file = request.files['file']
            if file.filename == '':
                return jsonify({"error": "No file"}), 400
            
            ext = file.filename.rsplit('.',1)[1].lower() if '.' in file.filename else 'jpg'
            with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}') as tmp:
                file.save(tmp.name)
                file_path = tmp.name
            
            threading.Thread(target=process_background, args=(file_path, None)).start()

        elif request.is_json:
            data = request.get_json()
            file_url = data.get('file_url') or data.get('fileUrl')
            if not file_url:
                return jsonify({"error": "No file_url"}), 400
            
            threading.Thread(target=process_background, args=(None, file_url)).start()
        
        else:
            return jsonify({"error": "Invalid request"}), 400

        return jsonify({"success": True, "message": "Processing..."}), 202

    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check"""
    pdf_support = False
    try:
        import fitz
        pdf_support = True
    except ImportError:
        pass
    
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "pdf_support": pdf_support,
        "base_id": AIRTABLE_BASE_ID,
        "table": AIRTABLE_TABLE_NAME
    }), 200

if __name__ == "__main__":
    print("\n🚀 Invoice Extractor API")
    print(f"📊 Base: {AIRTABLE_BASE_ID}")
    print(f"📋 Table: {AIRTABLE_TABLE_NAME}\n")
    
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
