"""
Invoice Extractor API for Softr Integration
This API receives invoice uploads from Softr and extracts data to Airtable
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import base64
import json
from openai import OpenAI
from pyairtable import Api
import tempfile
from datetime import datetime

app = Flask(__name__)
CORS(app)

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', 'your-openai-api-key')
AIRTABLE_API_KEY = os.getenv('AIRTABLE_API_KEY', 'your-airtable-token')
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID', 'your-base-id')
AIRTABLE_TABLE_NAME = os.getenv('AIRTABLE_TABLE_NAME', 'Invoices')

# Initialize clients
openai_client = OpenAI(api_key=OPENAI_API_KEY)
airtable_api = Api(AIRTABLE_API_KEY)
airtable_table = airtable_api.table(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME)


def extract_invoice_data(file_path):
    """Extract data from invoice using OpenAI Vision"""
    
    # Read and encode image
    with open(file_path, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode('utf-8')
    
    # Call OpenAI API
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
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        }
                    }
                ]
            }
        ],
        max_tokens=1000
    )
    
    # Parse response
    content = response.choices[0].message.content
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        content = content.split("```")[1].split("```")[0]
    
    return json.loads(content.strip())


def save_to_airtable(invoice_data):
    """Save extracted data to Airtable"""
    
    # Format line items
    line_items_text = "\n".join([
        f"{item.get('description', 'N/A')} - Qty: {item.get('quantity', 0)} × ${item.get('unit_price', 0)} = ${item.get('amount', 0)}"
        for item in invoice_data.get('line_items', [])
    ])
    
    # Prepare record
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
        "Currency": invoice_data.get("currency", "USD"),
        "Line Items": line_items_text
    }
    
    # Remove None values
    record = {k: v for k, v in record.items() if v is not None}
    
    # Create in Airtable
    created_record = airtable_table.create(record)
    return created_record


@app.route('/')
def home():
    """Health check"""
    return jsonify({
        "status": "active",
        "message": "Invoice Extractor API for Softr",
        "version": "1.0",
        "endpoints": {
            "/webhook": "POST - Receive invoice from Softr",
            "/health": "GET - Health check"
        }
    })


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Main endpoint for Softr webhook
    Accepts invoice file and processes it
    """
    try:
        print("📨 Received webhook request")
        
        # Method 1: File upload (multipart/form-data)
        if 'file' in request.files:
            file = request.files['file']
            
            if file.filename == '':
                return jsonify({"error": "No file selected"}), 400
            
            # Save temporarily
            file_ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'jpg'
            with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{file_ext}') as tmp:
                file.save(tmp.name)
                tmp_path = tmp.name
        
        # Method 2: JSON with file URL (if Softr sends URL)
        elif request.is_json:
            data = request.get_json()
            file_url = data.get('file_url') or data.get('attachment_url')
            
            if not file_url:
                return jsonify({"error": "No file_url provided"}), 400
            
            # Download file
            import requests
            response = requests.get(file_url)
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
                tmp.write(response.content)
                tmp_path = tmp.name
        
        else:
            return jsonify({"error": "No file provided. Send file or file_url"}), 400
        
        print(f"📄 Processing file: {tmp_path}")
        
        # Extract data
        print("🤖 Extracting data with AI...")
        invoice_data = extract_invoice_data(tmp_path)
        print(f"✅ Extracted: {invoice_data.get('invoice_number')}")
        
        # Save to Airtable
        print("💾 Saving to Airtable...")
        airtable_record = save_to_airtable(invoice_data)
        print(f"✅ Saved to Airtable: {airtable_record['id']}")
        
        # Clean up
        os.unlink(tmp_path)
        
        # Return success
        return jsonify({
            "success": True,
            "message": "Invoice processed successfully",
            "invoice_number": invoice_data.get("invoice_number"),
            "total_amount": invoice_data.get("total_amount"),
            "currency": invoice_data.get("currency"),
            "airtable_record_id": airtable_record['id'],
            "data": invoice_data
        }), 200
    
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/health', methods=['GET'])
def health():
    """Detailed health check"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "openai_configured": bool(OPENAI_API_KEY and OPENAI_API_KEY != 'your-openai-api-key'),
        "airtable_configured": bool(AIRTABLE_API_KEY and AIRTABLE_API_KEY != 'your-airtable-token'),
        "base_id": AIRTABLE_BASE_ID if AIRTABLE_BASE_ID != 'your-base-id' else 'not_configured'
    })


if __name__ == '__main__':
    print("\n" + "="*60)
    print("🚀 Invoice Extractor API for Softr")
    print("="*60)
    print(f"📊 Airtable Base: {AIRTABLE_BASE_ID}")
    print(f"📋 Table: {AIRTABLE_TABLE_NAME}")
    print(f"✅ Server running on http://0.0.0.0:5000")
    print("\n📌 Webhook endpoint: POST /webhook")
    print("="*60 + "\n")
    
    app.run(debug=True, host='0.0.0.0', port=5000)
