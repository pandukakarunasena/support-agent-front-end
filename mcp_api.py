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
        " - For u2_update_summary, use the product and product version.\n"
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
        self.awaiting_decision: bool = False

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

def get_wso2_token():
    url = WSO2_TOKEN_URL
    data = {"grant_type": "client_credentials"}
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "MyFlaskAppTest/1.0 (Flask/2.3.2)"
    }

    # Log what we’re about to send (but omit the secret itself)
    logger.info(
        f"POSTing to {url}\n"
        f"  data={data}\n"
        f"  headers-Accept={headers['Accept']}\n"
        f"  headers-User-Agent={headers['User-Agent']}\n"
        f"  auth=({WSO2_CLIENT_ID}, ****)"
    )

    try:
        response = requests.post(
            url,
            data=data,
            auth=(WSO2_CLIENT_ID, WSO2_CLIENT_SECRET),
            headers=headers
        )

        logger.info(f"Response from {url} → status={response.status_code}\n")

        response.raise_for_status()
        token = response.json().get("access_token")
        logger.info(f"Access token received: {token[:10]}")
        return token
    except Exception as e:
        logger.error(f"Failed to send request to {url}: {e}")
        return None
    
ACCESS_TOKEN_COOKIE = "access_token"
INTROSPECT_URL      = os.getenv("WSO2_INTROSPECT_URL", "")  # if you have an introspection endpoint

def get_token_with_credentials(username: str, password: str) -> Dict[str, Any]:

    data = {
        "grant_type": "password",
        "username":   username,
        "password":   password,
        "scope":      "openid"  # adjust scopes as needed
    }
    headers = {
        "Accept":       "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "MyFlaskAppTest/1.0 (Flask/2.3.2)"
    }
    try:
        resp = requests.post(
            WSO2_TOKEN_URL,
            data=data,
            auth=(WSO2_CLIENT_ID, WSO2_CLIENT_SECRET),
            headers=headers
        )
        return resp.json() if resp.status_code == 200 else {"error": "invalid_credentials"}
    except Exception as e:
        logger.error(f"Token request failed: {e}")
        return {"error": "token_endpoint_unreachable"}


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

@app.route("/login", methods=["POST"])
def login_post():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    if not username or not password:
        return render_template("login.html", error="Username and password are required.")

    token_json = get_token_with_credentials(username, password)
    if token_json.get("error"):
        return render_template("login.html", error="Invalid credentials or token endpoint error.")

    access_token = token_json.get("access_token")
    # expires_in   = token_json.get("expires_in", 3600)
    if not access_token:
        return render_template("login.html", error="No access token returned by server.")

    # Set cookie and redirect to chat UI
    resp = make_response(redirect(BASE_PATH + "/"))
    expire_date = os.environ.get("TOKEN_EXPIRE")  # or compute via datetime as shown before
    # For simplicity, set a session cookie that expires when browser closes:
    resp.set_cookie(
        ACCESS_TOKEN_COOKIE,
        access_token,
        httponly=True,
        secure=True,       # ensure HTTPS in production
        samesite="Strict"  # or "Lax" depending on your needs
    )
    return resp

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

    return redirect(f"{os.getenv("ASGARDEO_AUTH_ENDPOINT")}?{query}")

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
        samesite="Strict"
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
# @login_required
def home():
    # Renders your chat interface (index.html)
    return render_template("index.html")

# @app.route("/")
# def home():
#     return render_template("index.html")

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

        for product_entry in raw_data:
            product_name = product_entry.get("product-name")
            product_name = product_name.lower()
            if product_name not in PRODUCT_REPO_MAP:
                continue  # Skip if not in the map

            update_levels = product_entry.get("product-update-levels", [])

            result.append({
                "product": product_name,
                "versions": [
                    version_info.get("product-base-version")
                    for version_info in update_levels
                    if version_info.get("product-base-version")
                ]
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

        logger.info(f"[{cid}] Received request – User input: {user_input}, Product: {product_name}, Version: {product_version}")

        if not user_input:
            logger.warning(f"[{cid}] Empty input received")
            resp = make_response(jsonify({"error_code": "EMPTY_INPUT", "message": "Input is required"}), 400)
            resp.headers["X-Conversation-ID"] = cid
            return resp

        sess = sessions.setdefault(cid, Session(cid))

        if product_name:
            sess.history.append({"role": "user", "content": f"[product={product_name}]"})
            logger.info(f"[{cid}] Product added to session: {product_name}")
        if product_version:
            sess.history.append({"role": "user", "content": f"[version={product_version}]"})
            logger.info(f"[{cid}] Version added to session: {product_version}")

        sess.history.append({"role": "user", "content": user_input})
        logger.info(f"[{cid}] User input added to session history")

        chat_input = sess.history[1:]

        if sess.awaiting_decision and user_input.lower() in {"continue", "yes"}:
            sess.awaiting_decision = False
            logger.info(f"[{cid}] Resuming from tool decision...")
            response = oai.responses.create(
                model=MODEL,
                instructions=SYSTEM_MESSAGE["content"],
                input=chat_input,
                # tools=oai_tools,
                # tool_choice="auto",
            )
            logger.info(f"[{cid}] Tool response summary sent to user")
            output = response.output[0]
            if output.type == "message":
                resp = make_response(jsonify({
                    "conversation_id": cid,
                    "message": response.output[0].content[0].text,
                    "needs_more": False,
                    "hits": None
                }))
                resp.headers["X-Conversation-ID"] = cid
                return resp

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
        if tool_calls:
            logger.info(f"[{cid}] Tool calls detected: {[call.name for call in tool_calls]}")
            for call in tool_calls:
                args = json.loads(call.arguments or "{}")
                tool_name = call.name
                try:
                    # Ensure the conversation ID is included in the tool call arguments
                    args["cid"] = cid

                    # Pass the token to the u2 tool
                    if (tool_name == "u2_update_summary"):
                        logger.info(f"[{cid}] Adding access_token to tool call args")
                        args["access_token"] = request.cookies.get(ACCESS_TOKEN_COOKIE, "") 
                        # args["access_token"] = get_wso2_token()
                    
                        logger.info(
                            f"[{cid}] Executing tool: {tool_name} with args: "
                            f"{args['query']}, {args['product']}, {args['version']}, {args['cid']}, "
                            f"token-prefix={args['access_token'][:20]}"
                        )
                    else:
                        logger.info(f"[{cid}] Executing tool: {tool_name} with args: {args}")

                    result = mcp.call_tool(tool_name, args)
                    data = json.loads(result[0].text)
                    logger.info(f"[{cid}] Tool execution successful: {tool_name}")
                except Exception as e:
                    logger.info(f"[{cid}] Tool call failed: {tool_name} – {e}")
                    data = {"error": f"Tool `{tool_name}` failed", "error_code": "TOOL_CALL_ERROR"}

                hits = data if isinstance(data, list) else [data]
                # add only unique hits to the session
                # add_tool_results(sess, tool_name, hits)
                sess.hits.clear()
                sess.hits.append({"tool": tool_name, "results": hits})  # Clear previous hits before adding new ones
                sess.history.append({"role": "assistant", "content": json.dumps(sess.hits, separators=(",", ":"))})

                sess.awaiting_decision = True
                logger.info(f"[{cid}] Waiting for user decision to summarize tool output")
                resp = make_response(jsonify({
                    "conversation_id": cid,
                    "message": "Attached tools to Agent found below entries. You can get a summary by typing 'continue' or 'yes'.",
                    "needs_more": True,
                    "hits": sess.hits
                }))
                resp.headers["X-Conversation-ID"] = cid
                return resp

        message_chunks = [o for o in llm_resp.output if o.type == "message"]
        assistant_reply = "".join(c.text for c in message_chunks[0].content)
        sess.history.append({"role": "assistant", "content": assistant_reply})
        logger.info(f"[{cid}] Sending direct reply to user")
        resp = make_response(jsonify({
            "conversation_id": cid,
            "message": assistant_reply,
            "needs_more": False,
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
            "needs_more": False,
            "hits": None
        }), 500)
        resp.headers["X-Conversation-ID"] = cid
        return resp

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)