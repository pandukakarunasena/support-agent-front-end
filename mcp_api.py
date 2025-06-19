# Updated: api_server.py with log monitoring and specific error codes for chatbot frontend
import os
import uuid
import requests
import hashlib
import json
import logging
import logging.handlers


from dotenv import load_dotenv
from openai import OpenAI
from fastmcp_http.client import FastMCPHttpClient
import logging.handlers
from typing import Dict, List, Any
from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    redirect,
    url_for,
    make_response
)
from flask_cors import CORS
from dotenv import load_dotenv
from openai import OpenAI
from flask import render_template
from flask import make_response

# ─────────────────────────────── CONFIG ───────────────────────────────

# Logging setup
LOG_FILE = "logs/app.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5)
    ]
)
logger = logging.getLogger("mcp_flask")

# Load env
load_dotenv(override=True)
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL = "gpt-4o"

if os.getenv("ENVIRONMENT") == "development-choreo":
    MCP_SERVER_URL = f"{os.getenv('MCP_SERVER_HOST', 'localhost')}"
    BASE_PATH = os.getenv("BASE_PATH", "/")
else:
    MCP_SERVER_URL = f"http://{os.getenv('MCP_SERVER_HOST_LOCAL', 'localhost')}:{os.getenv('MCP_SERVER_PORT_LOCAL', '9999')}"
    BASE_PATH = ""

if not OPENAI_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in environment")

WSO2_TOKEN_URL = os.getenv("WSO2_TOKEN_URL")
WSO2_CLIENT_ID = os.getenv("WSO2_CLIENT_ID")
WSO2_CLIENT_SECRET = os.getenv("WSO2_CLIENT_SECRET")
WSO2_UPDATE_API = "https://apis.wso2.com/ocwn/updates-server/updates-803/v1.0/updates/product-update-levels"


oai = OpenAI(api_key=OPENAI_KEY)
mcp = FastMCPHttpClient(MCP_SERVER_URL)

# ─────────────────────────────── TOOLS ───────────────────────────────

tools_list = mcp.list_tools()
oai_tools = [
    {
        "name":        t.name,
        "description": t.description,
        "parameters":  t.inputSchema,
        "type":        "function"
    }
    for t in tools_list
]

PRODUCT_REPO_MAP = {
    "wso2is": "product-is",
    "wso2is-km": "product-is",
    "wso2mi": "product-micro-integrator",
    "wso2ei": "product-ei",
    "wso2am": "product-apim"
}


SYSTEM_MESSAGE = {
    "role": "system",
    "content": (
        "You are a troubleshooting agent. "
        "The user will describe an issue in free text. "
        "You have access to the following tools:\n"
        " • github_find_related_issues(term: str, top_k: int)\n"
        "   – Search GitHub Issues by exactly one **technical keyword** (e.g. NullPointerException).\n"
        " • u2_update_summary(product_version: str)\n"
        "   – Fetch the JSON summary of fixes/improvements for a specific product version.\n\n"
        "Before using any tools:\n"
        " - For u2_update_summary, use the product and product version. start update level and end update level\n"
        " - For github_find_related_issues, use the repository name derived from the product via PRODUCT_REPO_MAP.\n\n"
        "PRODUCT_REPO_MAP = {\n"
        "  \"wso2is\": \"product-is\",\n"
        "  \"wso2is-km\": \"product-is\",\n"
        "  \"wso2mi\": \"product-micro-integrator\",\n"
        "  \"wso2ei\": \"product-ei\",\n"
        "  \"wso2am\": \"product-apim\",\n"
        "}\n\n"
        "When the user describes a problem:\n"
        " 1. Decide which tool(s) to call.If user request a tool to check it must be executed. If no tool call is needed do a web search using web search tool available by default. The priority should go to the u2_update_summary tool, and ask if the user needs to get GitHub issues. If the user says GitHub issues are needed, run the github_find_related_issues tool.\n"
        " 2. If calling **github_find_related_issues**, extract **one** single-word technical term from their text and pass it as term.\n"
        " 3. If calling **u2_update_summary**, extract the product version (e.g. “v5.11.0”) and pass it as product_version; if missing, ask the user for it. Pass a summarized query to the tool to fetch entries as well.\n"
        " 4. If you need other data, prompt the user for it.\n"
        " 5. After each tool call, wait for the tool result before deciding to call more tools or to compose the final answer.\n"
        " 6. if the user input is continue or yes no tool calls should be executed. It must output the final summary of the data obtained from the tools calls without further tool call invocations.\n"
        " 7. If no tool is appropriate, answer directly.\n"
        " 8. If no related GitHub issues or summaries are found, tell the user “no related issue found.”"
    )
}



# ─────────────────────────────── SESSION ───────────────────────────────

class Session:
    def __init__(self, cid: str):
        self.id = cid
        self.history: List[Dict[str, Any]] = [SYSTEM_MESSAGE.copy()]
        self.hits: List[Dict[str, Any]] = []
        # self.awaiting_decision: bool = False

sessions: Dict[str, Session] = {}

# ─────────────────────────────── FLASK APP ───────────────────────────────

app = Flask(__name__)
CORS(app)

def hash_entry(entry):
    """Create a hash for a given hit result (based on JSON content)."""
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()

def add_tool_results(sess, tool_name, hits):
    existing_tool_entry = next((entry for entry in sess.hits if entry["tool"] == tool_name), None)

    new_hashes = {hash_entry(hit) for hit in hits}

    if existing_tool_entry:
        existing_hashes = {hash_entry(hit) for hit in existing_tool_entry["results"]}
        unique_hits = [hit for hit in hits if hash_entry(hit) not in existing_hashes]
        if unique_hits:
            existing_tool_entry["results"].extend(unique_hits)
    else:
        sess.hits.append({"tool": tool_name, "results": hits})

    
ACCESS_TOKEN_COOKIE = "access_token"
INTROSPECT_URL      = os.getenv("WSO2_INTROSPECT_URL", "")  # if you have an introspection endpoint


def login_required(f):
    """
    Decorator: checks for a valid access_token cookie. If missing or invalid,
    redirects to /login. If token introspection fails or token is inactive,
    clears the cookie and redirects to /login.
    """
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        # logger.info("script_root: " + request.script_root)
        # logger.info("url: " + request.url)
        # logger.info("url_root: " + request.url_root)
        # logger.info("path: " + request.path)
        # logger.info("full_path: " + request.full_path)
        # logger.info("method: " + request.root_path)

        token = request.cookies.get(ACCESS_TOKEN_COOKIE)
        if not token:
            return redirect(BASE_PATH + "/login")

        # Optional: If you have an introspection endpoint, call it:
        if INTROSPECT_URL:
            try:
                introspect = requests.post(
                    INTROSPECT_URL,
                    data={"token": token},
                    auth=(WSO2_CLIENT_ID, WSO2_CLIENT_SECRET),
                    headers={"Accept": "application/json"}
                )
                data = introspect.json()
                if not data.get("active"):
                    # token is invalid/expired
                    logger.info("script_root: " + request.script_root)
                    response = make_response(redirect(BASE_PATH + "/login"))
                    response.set_cookie(ACCESS_TOKEN_COOKIE, "", expires=0)
                    return response
            except Exception:
                # treat introspection errors as “not logged in”
                response = make_response(redirect(BASE_PATH + "/login"))
                response.set_cookie(ACCESS_TOKEN_COOKIE, "", expires=0)
                return response

        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET"])
def login():
    return render_template("login.html")  # see below

@app.route("/authlogin", methods=["POST"])
def auth_redirect():
    import urllib.parse

    client_id = os.getenv("WSO2_CLIENT_ID")
    redirect_uri = os.getenv("APP_REDIRECT_URI", f"{request.url_root.rstrip('/')}{BASE_PATH}/authcallback")
    scope = "openid profile"
    response_type = "code"
    state = str(uuid.uuid4())  # Optional, for CSRF protection

    query = urllib.parse.urlencode({
        "response_type": response_type,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
    })

    return redirect(os.getenv("ASGARDEO_AUTH_ENDPOINT") + "?" + query)

@app.route("/authcallback")
def auth_callback():
    import requests

    code = request.args.get("code")
    if not code:
        return render_template("login.html", error="Authorization failed or denied.")

    # Exchange code for tokens
    token_url = os.getenv("WSO2_TOKEN_URL", "https://api.asgardeo.io/t/wso2/oauth2/token")
    redirect_uri = os.getenv("APP_REDIRECT_URI", f"{request.url_root.rstrip('/')}{BASE_PATH}/authcallback")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": os.getenv("WSO2_CLIENT_ID"),
        "client_secret": os.getenv("WSO2_CLIENT_SECRET"),  
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token_response = requests.post(token_url, data=data, headers=headers)

    if token_response.status_code != 200:
        return render_template("login.html", error="Token exchange failed.")

    token_json = token_response.json()
    access_token = token_json.get("access_token")

    if not access_token:
        return render_template("login.html", error="Access token missing in response.")

    # Set access token as cookie
    resp = make_response(redirect(BASE_PATH + "/"))
    resp.set_cookie(
        ACCESS_TOKEN_COOKIE,
        access_token,
        httponly=True,
        secure=True,
        samesite="Lax",
        path=BASE_PATH
    )
    return resp

@app.route("/logout")
@login_required
def logout():
    resp = make_response(redirect(BASE_PATH + "/login"))
    resp.set_cookie(ACCESS_TOKEN_COOKIE, "", expires=0)
    return resp

# ── CHAT UI (INDEX) ──────────────────────────────────────────────────────

@app.route("/")
@login_required
def home():
    # Renders your chat interface (index.html)
    return render_template("index.html")


@app.route("/product-versions", methods=["GET"])
@login_required
def fetch_product_versions():
    token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    # token = get_wso2_token()
    if not token:
        return render_template("login.html", error="No access token returned by server.")

    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "PostmanRuntime/7.42.0"
            # "User-Agent": "MyFlaskAppTest/1.0 (Flask/2.3.2)"
        }

        logger.info(
            f"POSTing to {WSO2_UPDATE_API}\n"
            f"  headers-Accept={headers['Accept']}\n"
            f"  headers-User-Agent={headers['User-Agent']}\n"
            f"  headers-Content-Type={headers['Content-Type']}\n"
            f"  auth=({WSO2_CLIENT_ID}, ****)"
        )

        response = requests.get(WSO2_UPDATE_API, headers=headers)

        logger.info(
            f"Response from {WSO2_UPDATE_API} → status={response.status_code}\n"
            f"  body={response.text[:100]}"
        )

        response.raise_for_status()
        raw_data = response.json()
        result = []
        # logger.info(f"[INIT] raw_data: {raw_data[:100]}")

        result = []

        for product_entry in raw_data:
            product_name = product_entry.get("product-name")
            product_name = product_name.lower()
            if product_name not in PRODUCT_REPO_MAP:
                continue  # Skip if not in the map

            update_levels = product_entry.get("product-update-levels", [])

            # Now include update-levels per version
            versions = []
            for version_info in update_levels:
                base_version = version_info.get("product-base-version")
                if base_version:
                    versions.append({
                        "version": base_version,
                        "update_levels": version_info.get("update-levels", [])
                    })

            result.append({
                "product": product_name,
                "versions": versions
            })

        return jsonify(result)

    except Exception as e:
        logger.error(f"Products fetch failed: {e}")
        return jsonify({"error": "Failed to fetch products"}), 500
    
@app.route("/chat", methods=["POST"])
@login_required
def chat_endpoint():
    try:
        req = request.get_json()
        cid = request.headers.get("X-Conversation-ID") or req.get("conversation_id") or str(uuid.uuid4())
        user_input = req.get("user_input", "").strip()
        product_name = req.get("product")
        product_version = req.get("version")
        start_u_level = req.get("start_u_level", None)
        end_u_level = req.get("end_u_level", None)

        logger.info(f"[{cid}] Received request – User input: {user_input}, Product: {product_name}, Version: {product_version}, Start U-Level: {start_u_level}, End U-Level: {end_u_level}")    

        if not user_input:
            logger.warning(f"[{cid}] Empty input received")
            resp = make_response(jsonify({"error_code": "EMPTY_INPUT", "message": "Input is required"}), 400)
            resp.headers["X-Conversation-ID"] = cid
            return resp
        if not product_name:    
            logger.warning(f"[{cid}] No product specified")
            resp = make_response(jsonify({"error_code": "NO_PRODUCT", "message": "Product is required"}), 400)
            resp.headers["X-Conversation-ID"] = cid
            return resp
        if not product_version:
            logger.warning(f"[{cid}] No product version specified")
            resp = make_response(jsonify({"error_code": "NO_VERSION", "message": "Product version is required"}), 400)
            resp.headers["X-Conversation-ID"] = cid
            return resp
        if not start_u_level:
            logger.warning(f"[{cid}] No start U-Level specified")
            resp = make_response(jsonify({"error_code": "NO_START_U_LEVEL", "message": "Start U-Level is required"}), 400)
            resp.headers["X-Conversation-ID"] = cid
            return resp
        if not end_u_level:
            logger.warning(f"[{cid}] No end U-Level specified")
            resp = make_response(jsonify({"error_code": "NO_END_U_LEVEL", "message": "End U-Level is required"}), 400)
            resp.headers["X-Conversation-ID"] = cid
            return resp

        sess = sessions.setdefault(cid, Session(cid))
        sess.hits.clear()  # Clear hits at the start of the turn

        # Add context to history
        if product_name:
            sess.history.append({"role": "user", "content": f"[product={product_name}]"})
        if product_version:
            sess.history.append({"role": "user", "content": f"[version={product_version}]"})
        if start_u_level:   
            sess.history.append({"role": "user", "content": f"[start_u_level={start_u_level}]"})
        if end_u_level:
            sess.history.append({"role": "user", "content": f"[end_u_level={end_u_level}]"})

        sess.history.append({"role": "user", "content": user_input})

        chat_input = sess.history[1:]

        logger.info(f"[{cid}] Sending input to LLM")
        llm_resp = oai.responses.create(
            model=MODEL,
            instructions=SYSTEM_MESSAGE["content"],
            input=chat_input,
            tools=oai_tools,
            tool_choice="auto",
        )
        logger.info(f"[{cid}] Received response from LLM")

        tool_calls = [o for o in llm_resp.output if o.type == "function_call"]

        # If there are tool calls, execute and summarize
        if tool_calls:
            all_hits = []
            for call in tool_calls:
                args = json.loads(call.arguments or "{}")
                tool_name = call.name
                try:
                    args["cid"] = cid
                    if tool_name == "u2_update_summary":
                        args["access_token"] = request.cookies.get(ACCESS_TOKEN_COOKIE, "")
                        args["query"] = user_input
                    logger.info(f"[{cid}] Executing tool: {tool_name} with args: {args}")
                    result = mcp.call_tool(tool_name, args)
                    data = json.loads(result[0].text)
                    hits = data if isinstance(data, list) else [data]
                except Exception as e:
                    logger.info(f"[{cid}] Tool call failed: {tool_name} – {e}")
                    hits = [{"error": f"Tool `{tool_name}` failed", "error_code": "TOOL_CALL_ERROR"}]

                tool_hit = {"tool": tool_name, "results": hits}
                all_hits.append(tool_hit)

            sess.hits = all_hits
            # Add tool results to session history as user message (for LLM context

            tool_results_msg = (
                "Based on the following tool results (in JSON):\n"
                f"{json.dumps(sess.hits, indent=2)}\n\n"
                "Compose your response in three sections: 'Analysis', 'Tool Results Used', and 'Conclusion'.\n"
                "  - In 'Analysis', summarize the main technical issue, referencing relevant evidence from the tool results.\n"
                "  - In 'Tool Results Used', list which specific tool entries informed your analysis (use IDs, titles, or clear descriptors).\n"
                "  - In 'Conclusion', provide a clear, actionable summary or fix, suitable for a technical user.\n"
                "Be concise, do not repeat raw JSON, and make your reasoning explicit."
            )


            sess.history.append({"role": "user", "content": tool_results_msg})

            logger.info(f"[{cid}] Sending tool results and summary prompt to LLM")
            summary_resp = oai.responses.create(
                model=MODEL,
                instructions=SYSTEM_MESSAGE["content"],
                input=sess.history[1:],
            )
            logger.info(f"[{cid}] Got summary from LLM")

            summary_text = ""
            message_chunks = [o for o in summary_resp.output if o.type == "message"]
            if message_chunks:
                summary_text = "".join(c.text for c in message_chunks[0].content)
            else:
                summary_text = "No summary could be generated."

            sess.history.append({"role": "assistant", "content": summary_text})
            resp = make_response(jsonify({
                "conversation_id": cid,
                "message": summary_text,
                "hits": sess.hits
            }))
            resp.headers["X-Conversation-ID"] = cid
            return resp

        # Otherwise, just reply with LLM's answer
        message_chunks = [o for o in llm_resp.output if o.type == "message"]
        assistant_reply = "".join(c.text for c in message_chunks[0].content)
        sess.history.append({"role": "assistant", "content": assistant_reply})
        logger.info(f"[{cid}] Sending direct reply to user")
        resp = make_response(jsonify({
            "conversation_id": cid,
            "message": assistant_reply,
            "hits": None
        }))
        resp.headers["X-Conversation-ID"] = cid
        return resp

    except Exception as e:
        logger.exception(f"[{cid}] Chat processing failed")
        resp = make_response(jsonify({
            "error_code": "CHAT_PROCESSING_ERROR",
            "message": "Sorry, something went wrong.",
            "conversation_id": req.get("conversation_id", "unknown"),
            "hits": None
        }), 500)
        resp.headers["X-Conversation-ID"] = cid
        return resp

    
@app.route("/feedback", methods=["POST"])
@login_required
def feedback_endpoint():
    try:
        data = request.get_json()
        conversation_id = data.get("conversation_id", "unknown")
        tool = data.get("tool", "unknown")
        issue_url = data.get("issue_url", "N/A")
        description = (data.get("description") or "").strip().replace("\n", " ")
        related = data.get("related")

        logger.info(
            f"[{conversation_id}] FEEDBACK RECEIVED – Tool: {tool}, URL: {issue_url}, Related: {related}, Desc: {description[:200]}"
        )

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.exception("Failed to log feedback")
        return jsonify({"error": "Failed to log feedback"}), 500

@app.after_request
def apply_csp(response):
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src-elem 'self' 'unsafe-inline' cdn.jsdelivr.net cdnjs.cloudflare.com www.google.com www.gstatic.com; "
        "style-src-elem 'self' 'unsafe-inline' cdn.jsdelivr.net cdnjs.cloudflare.com fonts.googleapis.com; "
        "font-src 'self' fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )
    return response

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)