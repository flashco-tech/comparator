from flask import Flask, render_template, request, session, jsonify, Response, stream_with_context
from threading import Thread
from dotenv import load_dotenv
from tool_original import run_zepto, set_zepto_otp
from blinkit_tool_original import run_blinkit, set_blinkit_otp
from llm_parser import extract_products
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.schema import HumanMessage, SystemMessage, AIMessage
import re
import os
import json
import time

# Load env variables
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Flask setup
app = Flask(__name__)
app.secret_key = "AHSHh123"

# LangChain Gemini setup
llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-pro",
    temperature=0.7,
    google_api_key=GEMINI_API_KEY
)

# Global variables
results = {
    "blinkit": None,
    "zepto": None,
    "search_started": False,
    "otp_requested": False
}

# Helper functions
def extract_otp(text, vendor):
    match = re.search(rf"{vendor}[:\s]*([0-9]{{4,6}})", text, re.IGNORECASE)
    return match.group(1) if match else None

def extract_mobile(text):
    match = re.search(r"\b(\d{10})\b", text)
    return match.group(1) if match else None

def extract_address(text):
    # Try to find address explicitly mentioned
    match = re.search(r"address[:\s]*(.*)\b", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    
    # Try to find pin code or delivery pin code
    pin_match = re.search(r"(?:pin\s*code|delivery\s*pin\s*code)[:\s]*(\d{6})\b", text, re.IGNORECASE)
    if pin_match:
        return pin_match.group(1).strip()
    
    # Just look for a 6-digit number that might be a pin code
    pin_only = re.search(r"\b(\d{6})\b", text)
    return pin_only.group(1) if pin_only else None

def run_blinkit_thread(mobile, items, address):
    results["blinkit"] = run_blinkit(mobile, items, address)

def run_zepto_thread(mobile, items):
    results["zepto"] = run_zepto(mobile, items)

# System prompt for Gemini
SYSTEM_PROMPT = """
You are ShopSmart Assistant, a helpful grocery shopping assistant that helps users compare prices between Blinkit and Zepto.
Your job is to understand user requests and guide them through the shopping process.

You have access to the following functions:
1. extract_products(text) - Extracts product names from user text (handles various formats)
2. extract_mobile(text) - Extracts a 10-digit mobile number from text
3. extract_address(text) - Extracts an address or pin code from text
4. run_blinkit(mobile, items, address) - Searches for items on Blinkit
5. run_zepto(mobile, items) - Searches for items on Zepto
6. set_blinkit_otp(otp) - Sets the OTP for Blinkit
7. set_zepto_otp(otp) - Sets the OTP for Zepto

Follow this workflow:
1. When a user mentions products, extract them and confirm what they want to order
2. Ask for or confirm their mobile number and address (pin code)
3. Start the search process on both platforms call the functions run_zepto and run_blinkit in parallel
4. Guide them through entering OTPs when needed input the functions set_blinkit_otp and set_zepto_otp with appropriate otps.
5. Show a comparison of results when available when you will have both results. in json format from the functions.

Your responses should be in JSON format with a "type" field indicating the action needed:
- "text": Regular message to display
- "input_request": Request for specific information (mobile, address)
- "otp_request": Request for OTPs
- "comparison": Show comparison results

Always maintain context of the conversation and what information has already been collected.
"""

@app.route("/", methods=["GET"])
def index():
    if "messages" not in session:
        session["messages"] = []
        session["context"] = {
            "products": None,
            "mobile": None,
            "address": None,
            "workflow_state": "initial"
        }
    return render_template("chat.html")

@app.route("/stream", methods=["POST"])
def stream():
    """Unified stream endpoint that uses Gemini to orchestrate the entire workflow"""
    user_input = request.json.get("message", "")
    input_type = request.json.get("type", "text")
    
    # Initialize session if needed
    if "messages" not in session:
        session["messages"] = []
        session["context"] = {
            "products": None,
            "mobile": None,
            "address": None,
            "workflow_state": "initial"
        }
    
    # Add user message to history
    session["messages"].append({"role": "user", "content": user_input})
    session.modified = True
    
    # Process OTPs if provided
    if input_type == "blinkit_otp":
        set_blinkit_otp(user_input)
        session["context"]["blinkit_otp_provided"] = True
        session.modified = True
    
    if input_type == "zepto_otp":
        set_zepto_otp(user_input)
        session["context"]["zepto_otp_provided"] = True
        session.modified = True
    
    def generate_response():
        context = session.get("context", {})
        
        # Step 1: Check if we need to extract products
        if not context.get("products"):
            products = extract_products(user_input)
            if products:
                context["products"] = products
                session["context"] = context
                session.modified = True
                yield json.dumps({
                    "type": "text", 
                    "content": f"I found these items in your request: {', '.join(products)}"
                })
        
        # Step 2: Check if we need to get/confirm mobile number
        if context.get("products") and not context.get("mobile"):
            mobile = extract_mobile(user_input)
            if mobile:
                context["mobile"] = mobile
                session["context"] = context
                session.modified = True
                yield json.dumps({
                    "type": "text",
                    "content": f"Thanks! I'll use this mobile number: {mobile}"
                })
            else:
                yield json.dumps({
                    "type": "input_request",
                    "input_type": "mobile",
                    "content": "Please provide your 10-digit mobile number:"
                })
                return
        
        # Step 3: Check if we need to get/confirm address
        if context.get("products") and context.get("mobile") and not context.get("address"):
            address = extract_address(user_input) or (user_input if input_type == "address" else None)
            if address:
                context["address"] = address
                session["context"] = context
                session.modified = True
                yield json.dumps({
                    "type": "text",
                    "content": f"Got it! Using this delivery address: {address}"
                })
            else:
                yield json.dumps({
                    "type": "input_request",
                    "input_type": "address",
                    "content": "Please provide your delivery address:"
                })
                return
        
        # Step 4: Start search if we have all required info but haven't started yet
        if (context.get("products") and context.get("mobile") and context.get("address") and 
            not results["search_started"]):
            # Get the values from context
            products = context.get("products")
            mobile = context.get("mobile")
            address = context.get("address")
            
            results["search_started"] = True
            yield json.dumps({
                "type": "text",
                "content": "Starting search on Blinkit and Zepto. This might take a moment..."
            })
            def run_blinkit_thread():
                results["blinkit"] = run_blinkit(
                    mobile, products, address
                )
            def run_zepto_thread():
                results["zepto"] = run_zepto(
                    mobile, products
                )
            
            # Start search threads
            Thread(target=run_blinkit_thread).start()
            Thread(target=run_zepto_thread).start()
            
            # Request OTPs
            results["otp_requested"] = True
            yield json.dumps({
                "type": "otp_request",
                "content": "Please enter the OTPs you receive from Blinkit and Zepto:"
            })
            return
        
        # Step 5: Check if results are ready
        if results["search_started"] and results["blinkit"] and results["zepto"]:
            blinkit_total = results["blinkit"]["total"]
            zepto_total = results["zepto"]["total"]
            
            comparison_text = f"Here's your comparison:\n\n"
            comparison_text += f"**Blinkit Total: ₹{blinkit_total}**\nItems:\n"
            for item in results["blinkit"]["cart_items"]:
                comparison_text += f"- {item['name']}: ₹{item['price']}\n"
            
            comparison_text += f"\n**Zepto Total: ₹{zepto_total}**\nItems:\n"
            for item in results["zepto"]["cart_items"]:
                comparison_text += f"- {item['name']}: ₹{item['price']}\n"
            
            comparison_text += f"\n**Recommendation: "
            if blinkit_total < zepto_total:
                comparison_text += f"Blinkit is cheaper by ₹{zepto_total - blinkit_total}**"
            elif zepto_total < blinkit_total:
                comparison_text += f"Zepto is cheaper by ₹{blinkit_total - zepto_total}**"
            else:
                comparison_text += "Both have the same price**"
            
            yield json.dumps({
                "type": "comparison", 
                "content": comparison_text,
                "blinkit_data": results["blinkit"],
                "zepto_data": results["zepto"]
            })
            
            # Reset for next search
            results["blinkit"] = None
            results["zepto"] = None
            results["search_started"] = False
            results["otp_requested"] = False
            return
        
        # If we're waiting for OTPs, remind the user
        if results["otp_requested"] and (not results["blinkit"] or not results["zepto"]):
            # Check if we've received any OTPs from the input
            blinkit_otp = extract_otp(user_input, "blinkit")
            zepto_otp = extract_otp(user_input, "zepto")
            
            if blinkit_otp:
                set_blinkit_otp(blinkit_otp)
                yield json.dumps({
                    "type": "text", 
                    "content": "Blinkit OTP received. Processing..."
                })
            
            if zepto_otp:
                set_zepto_otp(zepto_otp)
                yield json.dumps({
                    "type": "text", 
                    "content": "Zepto OTP received. Processing..."
                })
            
            if not blinkit_otp and not zepto_otp:
                yield json.dumps({
                    "type": "text",
                    "content": "I'm still waiting for the OTPs from Blinkit and Zepto. Please enter them when you receive them."
                })
            return
        
        # Default: Use Gemini for general conversation
        messages = [SystemMessage(content=SYSTEM_PROMPT)]
        
        # Add conversation history and context
        context_msg = f"""
        Current context:
        - Products: {context.get('products')}
        - Mobile: {context.get('mobile')}
        - Address: {context.get('address')}
        - Search started: {results['search_started']}
        - OTP requested: {results['otp_requested']}
        - Blinkit results: {'Available' if results['blinkit'] else 'Not available'}
        - Zepto results: {'Available' if results['zepto'] else 'Not available'}
        """
        
        messages.append(SystemMessage(content=context_msg))
        
        for msg in session["messages"][-5:]:  # Only use last 5 messages for context
            if msg["role"] == "user":
                messages.append(HumanMessage(content=msg["content"]))
            else:
                messages.append(AIMessage(content=msg["content"]))
        
        try:
            ai_response = llm.invoke(messages)
            response_text = ai_response.content
            
            # Store AI response in session
            session["messages"].append({"role": "assistant", "content": response_text})
            session.modified = True
            
            # Stream the response
            yield json.dumps({"type": "text", "content": response_text})
        except Exception as e:
            yield json.dumps({"type": "text", "content": f"I'm having trouble processing your request. Please try again."})
    
    def format_sse(data):
        # Format as Server-Sent Events with proper data prefix
        return f"data: {data}\n\n"
    
    # Wrap the generator to format each chunk as SSE
    def sse_stream():
        for chunk in generate_response():
            yield format_sse(chunk)
    
    return Response(stream_with_context(sse_stream()), content_type='text/event-stream')

@app.route("/send_message", methods=["POST"])
def send_message():
    """Legacy endpoint that redirects to stream"""
    return stream()

@app.route("/reset", methods=["POST"])
def reset_chat():
    """Reset the chat session and results"""
    session.clear()
    results["blinkit"] = None
    results["zepto"] = None
    results["search_started"] = False
    results["otp_requested"] = False
    return jsonify({"status": "success"})

if __name__ == "__main__":
    app.run(debug=True)
