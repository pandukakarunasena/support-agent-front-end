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
    try:
        response = requests.post(
            WSO2_TOKEN_URL,
            data={
                "grant_type": "client_credentials"
            },
            auth=(WSO2_CLIENT_ID, WSO2_CLIENT_SECRET),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "PostmanRuntime/7.42.0"
            }
        )
        response.raise_for_status()
        return response.json()["access_token"]
    except Exception as e:
        logger.error(f"Token fetch failed: {e}")
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
            "User-Agent": "PostmanRuntime/7.42.0"
        }
        response = requests.get(WSO2_UPDATE_API, headers=headers)
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
                "message": "I found related entries. Reply with 'continue' to get a summary.",
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

    
# @app.route("/chat", methods=["POST"])
# def chat_endpoint():
#     try:
#         req = request.get_json()
#         cid = request.headers.get("X-Conversation-ID") or req.get("conversation_id") or str(uuid.uuid4())
#         user_input = req.get("user_input", "").strip()
#         product_name = req.get("product")
#         product_version = req.get("version")

#         logger.info(f"[{cid}] Received request – User input: {user_input}, Product: {product_name}, Version: {product_version}")

#         if not user_input:
#             logger.warning(f"[{cid}] Empty input received")
#             return jsonify({"error_code": "EMPTY_INPUT", "message": "Input is required"}), 400

#         sess = sessions.setdefault(cid, Session(cid))

#         if product_name:
#             sess.history.append({"role": "user", "content": f"[product={product_name}]"})
#             logger.info(f"[{cid}] Product added to session: {product_name}")
#         if product_version:
#             sess.history.append({"role": "user", "content": f"[version={product_version}]"})
#             logger.info(f"[{cid}] Version added to session: {product_version}")

#         sess.history.append({"role": "user", "content": user_input})
#         logger.info(f"[{cid}] User input added to session history")

#         chat_input = sess.history[1:]

#         # If resuming from tool call
#         if sess.awaiting_decision and user_input.lower() in {"continue", "yes"}:
#             sess.awaiting_decision = False
#             logger.info(f"[{cid}] Resuming from tool decision...")
#             response = oai.responses.create(
#                 model=MODEL,
#                 instructions=SYSTEM_MESSAGE["content"],
#                 input=chat_input,
#                 tools=oai_tools,
#                 tool_choice="auto",
#             )
#             logger.info(f"[{cid}] Tool response summary sent to user")
#             return jsonify({
#                 "conversation_id": cid,
#                 "message": response.output[0].content[0].text,
#                 "needs_more": False,
#                 "hits": None
#             })

#         # Normal flow
#         logger.info(f"[{cid}] Sending input to LLM")
#         llm_resp = oai.responses.create(
#             model=MODEL,
#             instructions=SYSTEM_MESSAGE["content"],
#             input=chat_input,
#             tools=oai_tools,
#             tool_choice="auto",
#         )
#         logger.info(f"[{cid}] Received response from LLM")

#         tool_calls = [o for o in llm_resp.output if o.type == "function_call"]
#         if tool_calls:
#             logger.info(f"[{cid}] Tool calls detected: {[call.name for call in tool_calls]}")
#             for call in tool_calls:
#                 args = json.loads(call.arguments or "{}")
#                 tool_name = call.name
#                 try:
#                     logger.info(f"[{cid}] Executing tool: {tool_name} with args: {args}")
#                     result = mcp.call_tool(tool_name, args)
#                     data = json.loads(result[0].text)
#                     logger.info(f"[{cid}] Tool execution successful: {tool_name}")
#                 except Exception as e:
#                     logger.error(f"[{cid}] Tool call failed: {tool_name} – {e}")
#                     data = {"error": f"Tool `{tool_name}` failed", "error_code": "TOOL_CALL_ERROR"}

#                 hits = data if isinstance(data, list) else [data]
#                 sess.hits.append({"tool": tool_name, "results": hits})
#                 sess.history.append({"role": "assistant", "content": json.dumps(sess.hits, separators=(",", ":"))})

#             sess.awaiting_decision = True
#             logger.info(f"[{cid}] Waiting for user decision to summarize tool output")
#             return jsonify({
#                 "conversation_id": cid,
#                 "message": "I found related entries. Reply with 'continue' to get a summary.",
#                 "needs_more": True,
#                 "hits": sess.hits
#             })

#         # No tool usage
#         message_chunks = [o for o in llm_resp.output if o.type == "message"]
#         assistant_reply = "".join(c.text for c in message_chunks[0].content)
#         sess.history.append({"role": "assistant", "content": assistant_reply})
#         logger.info(f"[{cid}] Sending direct reply to user")
#         return jsonify({
#             "conversation_id": cid,
#             "message": assistant_reply,
#             "needs_more": False,
#             "hits": None
#         })

#     except Exception as e:
#         logger.exception(f"[{cid}] Chat processing failed")
#         return jsonify({
#             "error_code": "CHAT_PROCESSING_ERROR",
#             "message": "Sorry, something went wrong.",
#             "conversation_id": req.get("conversation_id", "unknown"),
#             "needs_more": False,
#             "hits": None
#         }), 500


# @app.route("/chat", methods=["POST"])
# def chat_endpoint():
#     try:
#         req = request.get_json()
#         cid = request.headers.get("X-Conversation-ID") or req.get("conversation_id") or str(uuid.uuid4())
#         user_input = req.get("user_input", "").strip()
#         product_name = req.get("product")
#         product_version = req.get("version")

#         logger.info(f"[{cid}] User input: {user_input}, Product: {product_name}, Version: {product_version}")

#         if not user_input:
#             return jsonify({"error_code": "EMPTY_INPUT", "message": "Input is required"}), 400

#         sess = sessions.setdefault(cid, Session(cid))

#         # Optional: Add product/version to session or history
#         if product_name:
#             sess.history.append({"role": "user", "content": f"[product={product_name}]"})
#         if product_version:
#             sess.history.append({"role": "user", "content": f"[version={product_version}]"})

#         sess = sessions.setdefault(cid, Session(cid))
#         sess.history.append({"role": "user", "content": user_input})
#         chat_input = sess.history[1:]

#         # If resuming from tool call
#         if sess.awaiting_decision and user_input.lower() in {"continue", "yes"}:
#             sess.awaiting_decision = False
#             response = oai.responses.create(
#                 model=MODEL,
#                 instructions=SYSTEM_MESSAGE["content"],
#                 input=chat_input,
#                 tools=oai_tools,
#                 tool_choice="auto",
#             )
#             return jsonify({
#                 "conversation_id": cid,
#                 "message": response.output[0].content[0].text,
#                 "needs_more": False,
#                 "hits": None
#             })

#         # Normal flow
#         llm_resp = oai.responses.create(
#             model=MODEL,
#             instructions=SYSTEM_MESSAGE["content"],
#             input=chat_input,
#             tools=oai_tools,
#             tool_choice="auto",
#         )

#         tool_calls = [o for o in llm_resp.output if o.type == "function_call"]
#         if tool_calls:
#             for call in tool_calls:
#                 args = json.loads(call.arguments or "{}")
#                 tool_name = call.name
#                 try:
#                     result = mcp.call_tool(tool_name, args)
#                     data = json.loads(result[0].text)
#                 except Exception as e:
#                     logger.error(f"Tool call failed: {tool_name} – {e}")
#                     data = {"error": f"Tool `{tool_name}` failed", "error_code": "TOOL_CALL_ERROR"}

#                 hits = data if isinstance(data, list) else [data]
#                 sess.hits.append({"tool": tool_name, "results": hits})
#                 sess.history.append({"role": "assistant", "content": json.dumps(sess.hits, separators=(",", ":"))})

#             sess.awaiting_decision = True
#             return jsonify({
#                 "conversation_id": cid,
#                 "message": "I found related entries. Reply with 'continue' to get a summary.",
#                 "needs_more": True,
#                 "hits": sess.hits
#             })

#         # No tool usage
#         message_chunks = [o for o in llm_resp.output if o.type == "message"]
#         assistant_reply = "".join(c.text for c in message_chunks[0].content)
#         sess.history.append({"role": "assistant", "content": assistant_reply})
#         return jsonify({
#             "conversation_id": cid,
#             "message": assistant_reply,
#             "needs_more": False,
#             "hits": None
#         })

#     except Exception as e:
#         logger.exception("Chat processing failed")
#         return jsonify({
#             "error_code": "CHAT_PROCESSING_ERROR",
#             "message": "Sorry, something went wrong.",
#             "conversation_id": req.get("conversation_id", "unknown"),
#             "needs_more": False,
#             "hits": None
#         }), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)


# PRODUCT_REPO_MAP = {
#   "wso2is":     "product-is",
#   "wso2is-km":  "product-is",
#   "wso2mi":     "product-micro-integrator",
#   "wso2ei":     "product-ei",
#   "wso2am":     "product-apim",
# }

# # Logging Setup with file and rotation
# LOG_FILE = "logs/app.log"
# os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
#     handlers=[
#         logging.StreamHandler(),
#         logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5)
#     ]
# )
# logger = logging.getLogger("mcp_api")

# # Configuration
# load_dotenv(override=True)

# MODEL = "gpt-4o"
# OPENAI_KEY = os.getenv("OPENAI_API_KEY")
# MCP_SERVER_HOST = os.getenv("MCP_SERVER_HOST", "localhost")
# MCP_SERVER_PORT = os.getenv("MCP_SERVER_PORT", "9999")
# MCP_SERVER_URL = f"http://{MCP_SERVER_HOST}:{MCP_SERVER_PORT}"

# if not OPENAI_KEY:
#     raise RuntimeError("Missing OPENAI_API_KEY in environment")

# oai = OpenAI(api_key=OPENAI_KEY)
# mcp = FastMCPHttpClient(MCP_SERVER_URL)

# # Tools
# tools_list = mcp.list_tools()
# oai_tools = [
#     {
#         "name":        t.name,
#         "description": t.description,
#         "parameters":  t.inputSchema,
#         "type":        "function"
#     }
#     for t in tools_list
# ]

# SYSTEM_MESSAGE = {
#     "role": "system",
#     "content": (
#         "You are a troubleshooting agent. "
#         "The user will describe an issue in free text. "
#         "You have access to the following tools:\n"
#         " • github_find_related_issues(term: str, top_k: int)\n"
#         "   – Search GitHub Issues by exactly one **technical keyword** (e.g. NullPointerException).\n"
#         " • u2_update_summary(product_version: str)\n"
#         "   – Fetch the JSON summary of fixes/improvements for a specific product version.\n\n"
#         "Before using any tools:\n"
#         " - Always ask the user for the **product** and **product version**.\n"
#         " - Validate the product using the `PRODUCT_REPO_MAP` JSON object, where the key is the product and the value is the GitHub repository name.\n"
#         " - For `u2_update_summary`, use the product and product version.\n"
#         " - For `github_find_related_issues`, use the repository name derived from the product via `PRODUCT_REPO_MAP`.\n\n"
#         "When the user describes a problem:\n"
#         " 1. Decide which tool(s) to call.\n"
#         " 2. If calling **github_find_related_issues**, extract **one** single‐word technical term from their text and pass it as `term`.\n"
#         " 3. If calling **u2_update_summary**, extract the product version (e.g. “v5.11.0”) and pass it as `product_version`; if missing, ask the user for it. Pass a summarized query to the tool to fetch entries as well.\n"
#         " 4. If you need other data, prompt the user for it.\n"
#         " 5. After each tool call, wait for the tool result before deciding to call more tools or to compose the final answer.\n"
#         " 6. If no tool is appropriate, answer directly.\n"
#         " 7. If no related GitHub issues or summaries are found, tell the user “no related issue found.”"
#     )
# }

# class Session:
#     def __init__(self, cid: str):
#         self.id = cid
#         self.history: List[Dict[str, Any]] = [SYSTEM_MESSAGE.copy()]
#         self.hits: List[Dict[str, Any]] = []
#         self.awaiting_decision: bool = False

# sessions: Dict[str, Session] = {}

# class ChatRequest(BaseModel):
#     conversation_id: Optional[str] = None
#     user_input: str


# # def stream_response(openai_stream):
# #     try:
# #         for chunk in openai_stream:
# #             if chunk.choices[0].delta.content:
# #                 yield chunk.choices[0].delta.content
# #     except Exception as e:
# #         logger.error(f"Streaming error: {e}")
# #         yield "[Error] Failed to stream the response."

# app = FastAPI()

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# @app.exception_handler(Exception)
# def general_exception_handler(request: Request, exc: Exception):
#     logger.exception(f"Unexpected error at {request.url.path}: {exc}")
#     return JSONResponse(
#         status_code=500,
#         content={
#             "error_code": "SERVER_ERROR",
#             "message": "Oops! Something went wrong. Please try again later."
#         },
#     )

# @app.post("/chat")
# def chat_endpoint(req: ChatRequest):
#     try:
#         cid = req.conversation_id or str(uuid.uuid4())
#         sess = sessions.setdefault(cid, Session(cid))

#         user_txt = req.user_input.strip()
#         sess.history.append({"role": "user", "content": user_txt})

#         # 👉 Pull the system prompt and the rest of the history just once
#         system_instr = SYSTEM_MESSAGE["content"]
#         # sess.history[1:] is already a List[{"role":..., "content":...}]
#         chat_input    = sess.history[1:]

#         # 1) If we’re resuming after tool hits
#         if sess.awaiting_decision:
#             if user_txt.lower() in {"continue", "yes"}:
#                 sess.awaiting_decision = False
#                 response = oai.responses.create(
#                     model=MODEL,
#                     instructions=system_instr,
#                     input=chat_input,
#                     # stream=True,
#                     tools=oai_tools,
#                     tool_choice="auto",
#                 )
#                 # return StreamingResponse(stream_response(stream), media_type="text/plain")
#                 return  {
#                     "conversation_id": cid,
#                     "message":         response.output[0].content[0].text,
#                     "needs_more":      False,
#                     "hits":            None,
                    
#                 }
#             else:
#                 sess.awaiting_decision = False  # user gave new detail, loop back into normal flow

#         # 2) Normal one‐shot call
#         llm_resp = oai.responses.create(
#             model=MODEL,
#             instructions=system_instr,
#             input=chat_input,
#             tools=oai_tools,
#             tool_choice="auto",
#         )

#         # 3) Detect and execute any tool_call outputs
#         tool_calls = [o for o in llm_resp.output if o.type == "function_call"]
#         if tool_calls:
#             for call in tool_calls:
#                 args      = json.loads(call.arguments or "{}")
#                 tool_name = call.name
#                 try:
#                     result = mcp.call_tool(tool_name, args)
#                     data   = json.loads(result[0].text)
#                 except Exception as e:
#                     logger.error(f"Tool call failed: {tool_name} – {e}")
#                     data = {"error": f"Tool `{tool_name}` failed.", "error_code": "TOOL_CALL_ERROR"}

#                 hits = data if isinstance(data, list) else [data]
#                 sess.hits.append({"tool": tool_name, "results": hits})
#                 sess.history.append({
#                     "role":       "assistant",
#                     "content":    json.dumps(sess.hits, separators=(",", ":")),
#                 })

#             sess.awaiting_decision = True
#             return {
#                 "conversation_id": cid,
#                 "message": (
#                     "I found these related entries. Reply with \"continue\" for a summary,"
#                     " or provide more details." if sess.hits else "No entries found—try another query."
#                 ),
#                 "needs_more": True,
#                 "hits":       sess.hits,
#             }

#         # 4) No tools → stitch together the assistant’s text chunks
#         message_items   = [o for o in llm_resp.output if o.type == "message"]
#         assistant_text  = "".join(chunk.text for chunk in message_items[0].content)

#         sess.history.append({"role": "assistant", "content": assistant_text})
#         return {
#             "conversation_id": cid,
#             "message":         assistant_text,
#             "needs_more":      False,
#             "hits":            None,
#         }

#     except Exception as e:
#         logger.error(f"Chat processing error: {e}")
#         return JSONResponse(
#             status_code=500,
#             content={
#                 "error_code":       "CHAT_PROCESSING_ERROR",
#                 "conversation_id":  req.conversation_id or "unknown",
#                 "message":          "Sorry, something went wrong while processing your request. Please try again.",
#                 "needs_more":       False,
#                 "hits":             None,
#             },
#         )


# @app.post("/chat")
# def chat_endpoint(req: ChatRequest):
#     try:
#         cid = req.conversation_id or str(uuid.uuid4())
#         sess = sessions.setdefault(cid, Session(cid))

#         user_txt = req.user_input.strip()

#         # 1) Build the Responses API inputs:
#         system_instr = sess.history[0]["content"]
#         chat_input = [
#             {"role": m["role"], "content": m["content"]}
#             for m in sess.history[1:]
#         ]

#         logger.info(ch[{cid}] at_input)
            
#         # 2) Handle "continue" after tool results
#         if sess.awaiting_decision:
#             if user_txt.lower() in {"continue", "yes"}:
#                 sess.awaiting_decision = False

#                 stream = oai.responses.create(
#                     model=MODEL,
#                     instructions=SYSTEM_MESSAGE.content,
#                     input=chat_input,
#                     tools=oai_tools,            # ← still called "tools"
#                     tool_choice="auto",         # ← still "tool_choice"
#                 )
#                 return StreamingResponse(stream_response(stream), media_type="text/plain")
#             else:
#                 sess.history.append({"role": "user", "content": user_txt})
#                 sess.awaiting_decision = False

#         # 3) Append the user message
#         sess.history.append({"role": "user", "content": user_txt})

#         # 4) Single–round call with function/tool support
#         llm_resp = oai.responses.create(
#             model=MODEL,
#             instructions=SYSTEM_MESSAGE.content,
#             input=[
#                 {"role": m["role"], "content": m["content"]}
#                 for m in sess.history[1:]
#             ],
#             tools=oai_tools,                  # ← correct param
#             tool_choice="auto",               # ← correct param
#         )

#         # logger.info(ll[{cid}] m_resp)
#         msg = llm_resp.output

#         logger.info(f"[{cid}] llm_output {msg}")
#         tool_calls = [item for item in llm_resp.output if item.type == "tool_call"]

#         # 5) Tool-calling branch (unchanged)
#         if tool_calls:
#             # Loop through each tool call directive
#             logger.info(f"[{cid}] tool_calls: {tool_calls}")
#             for call in tool_calls:
#                 args = json.loads(call.arguments or "{}")
#                 tool_name = call.name
#                 try:
#                     result = mcp.call_tool(tool_name, args)
#                     data = json.loads(result[0].text)
#                 except Exception as e:
#                     logger.error(f"Tool call failed: {tool_name} - {e}")
#                     data = {"error": f"Tool `{tool_name}` failed.", "error_code": "TOOL_CALL_ERROR"}

#                 results = data if isinstance(data, list) else [data]
#                 sess.hits.append({"tool": tool_name, "results": results})

#                 # Record the tool‐call in history
#                 sess.history.append({
#                     "role":         "tool",
#                     "tool_call_id": call.id,
#                     "name":         tool_name,
#                     "content":      json.dumps(sess.hits, separators=(",", ":")),
#                 })

#             sess.awaiting_decision = True
#             return {
#                 "conversation_id": cid,
#                 "message": (
#                     "I found these related entries. Reply with \"continue\" for a summary, "
#                     "or provide more details."
#                     if sess.hits else
#                     "No entries found—try another query."
#                 ),
#                 "needs_more": True,
#                 "hits":       sess.hits,
#             }

#         # 6) No tools → return assistant text
#         assistant_text = msg[0].content[0].text
#         sess.history.append({"role": "assistant", "content": assistant_text})
#         return {
#             "conversation_id": cid,
#             "message":         assistant_text,
#             "needs_more":      False,
#             "hits":            None,
#         }

#     except Exception as e:
#         logger.error(f"Chat processing error: {e}")
#         return JSONResponse(
#             status_code=500,
#             content={
#                 "error_code":       "CHAT_PROCESSING_ERROR",
#                 "conversation_id":  req.conversation_id or "unknown",
#                 "message":          "Sorry, something went wrong while processing your request. Please try again.",
#                 "needs_more":       False,
#                 "hits":             None,
#             },
#         )



# if __name__ == "__main__":
#     uvicorn.run("mcp_api:app", host="localhost", port=8000, reload=True)



# Database setup for persistent conversation storage
#db_conn = sqlite3.connect("conversations.db", check_same_thread=False)
#db_cursor = db_conn.cursor()
#db_cursor.execute("""
# CREATE TABLE IF NOT EXISTS conversations (
#     conversation_id TEXT,
#     role TEXT,
#     content TEXT
# )
# """)
# db_conn.commit()

# def save_to_db(conversation_id: str, role: str, content: str):
#     try:
#         db_cursor.execute(
#             "INSERT INTO conversations (conversation_id, role, content) VALUES (?, ?, ?)",
#             (conversation_id, role, content)
#         )
#         db_conn.commit()
#     except Exception as e:
#         logger.error(f"Database error: {e}")

# @app.post("/chat")
# def chat_endpoint(req: ChatRequest):
#     try:
#         cid = req.conversation_id or str(uuid.uuid4())
#         sess = sessions.setdefault(cid, Session(cid))

#         user_txt = req.user_input.strip()

#         if sess.awaiting_decision:
#             if user_txt.lower() in {"continue", "yes"}:
#                 sess.awaiting_decision = False
#                 stream = oai.responses.create(
#                     model=MODEL,
#                     messages=sess.history,
#                     stream=True,
#                 )
#                 #save_to_db(cid, "user", user_txt)
#                 return StreamingResponse(stream_response(stream), media_type="text/plain")
#             else:
#                 sess.history.append({"role": "user", "content": user_txt})
#                 #save_to_db(cid, "user", user_txt)
#                 sess.awaiting_decision = False

#         sess.history.append({"role": "user", "content": user_txt})
#         #save_to_db(cid, "user", user_txt)

#         llm_resp = oai.responses.create(
#             model=MODEL,
#             messages=sess.history,
#             tools=oai_tools,
#             tool_choice="auto",
#         )

#         msg = llm_resp.choices[0].message

#         if msg.tool_calls:
#             sess.history.append(msg.model_dump())
#             for call in msg.tool_calls:
#                 args = json.loads(call.function.arguments or "{}")
#                 tool_name = call.function.name
#                 try:
#                     result = mcp.call_tool(tool_name, args)
#                     data = json.loads(result[0].text)
#                 except Exception as e:
#                     logger.error(f"Tool call failed: {tool_name} - {e}")
#                     data = {"error": f"Tool `{tool_name}` failed.", "error_code": "TOOL_CALL_ERROR"}

#                 results = data if isinstance(data, list) else [data]
#                 sess.hits.append({"tool": tool_name, "results": results})

#                 sess.history.append({
#                     "role": "tool",
#                     "tool_call_id": call.id,
#                     "name": tool_name,
#                     "content": json.dumps(sess.hits, separators=(",", ":")),
#                 })

#             sess.awaiting_decision = True
#             return {
#                 "conversation_id": cid,
#                 "message": ("I found these related entries. Reply with \"continue\" for a summary, or provide more details." if sess.hits else "No entries found—try another query."),
#                 "needs_more": True,
#                 "hits": sess.hits,
#             }

#         assistant_text = msg.content
#         sess.history.append({"role": "assistant", "content": assistant_text})
#         #save_to_db(cid, "assistant", assistant_text)
#         return {
#             "conversation_id": cid,
#             "message": assistant_text,
#             "needs_more": False,
#             "hits": None,
#         }
#     except Exception as e:
#         logger.error(f"Chat processing error: {e}")
#         return JSONResponse(
#             status_code=500,
#             content={
#                 "error_code": "CHAT_PROCESSING_ERROR",
#                 "conversation_id": req.conversation_id or "unknown",
#                 "message": "Sorry, something went wrong while processing your request. Please try again.",
#                 "needs_more": False,
#                 "hits": None,
#             },
#         )
