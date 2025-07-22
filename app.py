# app.py
from flask import Flask, request, render_template, session, redirect, url_for
from llm_parser import extract_products
from blinkit_tool_original import run_blinkit
from tool_original import run_zepto
from threading import Thread
from blinkit_tool_original import set_blinkit_otp
from tool_original import set_zepto_otp

app = Flask(__name__)
app.secret_key = "AHSHh123"  # Required for session handling

# Global results dictionary
results = {
    "blinkit": None,
    "zepto": None
}

@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        # Step 1: Extract product names from user input
        user_text = request.form["user_text"]
        search_items = extract_products(user_text)
        session["search_items"] = search_items
        return redirect(url_for("get_mobile"))
    return render_template("home.html")

@app.route("/get_mobile", methods=["GET", "POST"])
def get_mobile():
    if request.method == "POST":
        phone_number = request.form["mobile"]
        ADDRESS_TO_SEARCH = request.form["address"]
        session["phone_number"] = phone_number
        session["address"] = ADDRESS_TO_SEARCH
        # Get values from session before passing to threads
        search_items = session["search_items"]
        
        # Start Blinkit and Zepto in parallel
        def run_blinkit_thread():
            results["blinkit"] = run_blinkit(
                 phone_number,search_items,ADDRESS_TO_SEARCH
            )
        def run_zepto_thread():
            results["zepto"] = run_zepto(
                 phone_number,search_items
            )
        Thread(target=run_blinkit_thread).start()
        Thread(target=run_zepto_thread).start()

        return redirect(url_for("enter_otp"))
    return render_template("mobile.html")

@app.route("/enter_otp", methods=["GET", "POST"])
def enter_otp():
    if request.method == "POST":
        blinkit_otp = request.form.get("blinkit_otp")
        zepto_otp = request.form.get("zepto_otp")
        if blinkit_otp:
            set_blinkit_otp(blinkit_otp)
        if zepto_otp:
            set_zepto_otp(zepto_otp)
        return redirect(url_for("results_view"))
    return render_template("otp.html")

@app.route("/results")
def results_view():
    if not results["blinkit"] or not results["zepto"]:
        return redirect(url_for("loading"))
    return render_template("results.html",blinkit_data=results["blinkit"], zepto_data=results["zepto"])

@app.route("/loading")
def loading():
    if results["blinkit"] and results["zepto"]:
        return redirect(url_for("results_view"))
    return render_template("loading.html")


if __name__ == "__main__":
    app.run(debug=True)
