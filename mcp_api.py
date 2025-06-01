# Updated: api_server.py with log monitoring and specific error codes for chatbot frontend
import os
import uuid
import requests
import json
import logging
import logging.handlers


from dotenv import load_dotenv
from openai import OpenAI
from fastmcp_http.client import FastMCPHttpClient
import logging.handlers
from typing import Dict, List, Any
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from openai import OpenAI
from flask import render_template

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
else:
    MCP_SERVER_URL = f"http://{os.getenv('MCP_SERVER_HOST', 'localhost')}:{os.getenv('MCP_SERVER_PORT', '9999')}"

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
        " 1. Decide which tool(s) to call. The priority should go to the u2_update_summary tool, and ask if the user needs to get GitHub issues. If the user says GitHub issues are needed, run the github_find_related_issues tool.\n"
        " 2. If calling **github_find_related_issues**, extract **one** single-word technical term from their text and pass it as term.\n"
        " 3. If calling **u2_update_summary**, extract the product version (e.g. “v5.11.0”) and pass it as product_version; if missing, ask the user for it. Pass a summarized query to the tool to fetch entries as well.\n"
        " 4. If you need other data, prompt the user for it.\n"
        " 5. After each tool call, wait for the tool result before deciding to call more tools or to compose the final answer.\n"
        " 6. If no tool is appropriate, answer directly.\n"
        " 7. If no related GitHub issues or summaries are found, tell the user “no related issue found.”"
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
        "POSTing to %s\n  data=%s\n  headers=%s\n  auth=(%s, ****)",
        url,
        data,
        headers,
        WSO2_CLIENT_ID
    )

    try:
        response = requests.post(
            url,
            data=data,
            auth=(WSO2_CLIENT_ID, WSO2_CLIENT_SECRET),
            headers=headers
        )

        logger.info(
            "Response from %s → status=%s\n  body=%s",
            url,
            response.status_code,
            response.text
        )

        response.raise_for_status()
        token = response.json().get("access_token")
        logger.info("Access token received: %s", token[:10] )
        return token
    except Exception as e:
        logger.error("Failed to send request to %s: %s", url, e)
        return None


@app.route("/")
def home():
    return render_template("index.html")

@app.route("/product-versions", methods=["GET"])
def fetch_product_versions():
    token = get_wso2_token()
    if not token:
        return jsonify({"error": "Failed to obtain access token"}), 500

    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            # "User-Agent": "PostmanRuntime/7.42.0"
            "User-Agent": "MyFlaskAppTest/1.0 (Flask/2.3.2"
        }

        logger.info(
            "POSTing to %s\n headers=%s\n  auth=(%s, ****)",
            WSO2_UPDATE_API,
            headers,
            WSO2_CLIENT_ID
        )
        response = requests.get(WSO2_UPDATE_API, headers=headers)

        logger.info(
            "Response from %s → status=%s\n  body=%s",
            WSO2_UPDATE_API,
            response.status_code,
            response.text
        )

        response.raise_for_status()
        raw_data = response.json()  
        result = []
        logger.info(f"[INIT] raw_data: {raw_data}")

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
    

from flask import make_response

@app.route("/chat", methods=["POST"])
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
                tools=oai_tools,
                tool_choice="auto",
            )
            logger.info(f"[{cid}] Tool response summary sent to user")
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
                    logger.info(f"[{cid}] Executing tool: {tool_name} with args: {args}")
                    result = mcp.call_tool(tool_name, args)
                    data = json.loads(result[0].text)
                    logger.info(f"[{cid}] Tool execution successful: {tool_name}")
                except Exception as e:
                    logger.error(f"[{cid}] Tool call failed: {tool_name} – {e}")
                    data = {"error": f"Tool `{tool_name}` failed", "error_code": "TOOL_CALL_ERROR"}

                hits = data if isinstance(data, list) else [data]
                sess.hits.append({"tool": tool_name, "results": hits})
                sess.history.append({"role": "assistant", "content": json.dumps(sess.hits, separators=(",", ":"))})

                sess.awaiting_decision = True
                logger.info(f"[{cid}] Waiting for user decision to summarize tool output")
                resp = make_response(jsonify({
                    "conversation_id": cid,
                    "message": "Below Entries have been found from U2 and Github. You can get a summary by typing 'continue' or 'yes'.",
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