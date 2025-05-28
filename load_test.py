#!/usr/bin/env python3
import argparse
import asyncio
import aiohttp
import time
import uuid
import statistics
import sys
import random


# map of products → available versions


async def send_chat(session, url, message):
    """
    Send one chat request, return (status, elapsed_time).
    """
    PRODUCT_VERSIONS = {
        "wso2is":       ["5.8.0", "5.9.0", "5.10.0", "5.11.0", "6.1.0", "7.0.0", "7.1.0"],
        "wso2is-km":    ["5.8.0", "5.9.0", "5.10.0"],
        "wso2mi":       ["4.4.0", "4.1.0", "4.0.0", "4.2.0", "4.3.0"],
        "wso2ei":       ["6.4.0", "6.0.0", "6.5.0", "6.1.1", "6.2.0", "6.3.0", "6.6.0"],
        "wso2am":       ["4.2.0", "3.1.0", "3.0.0", "3.2.0", "3.2.1", "4.1.0", "4.3.0", "4.5.0"],
    }
    
    cid = str(uuid.uuid4())
    product = random.choice(list(PRODUCT_VERSIONS))
    print(f"[{cid}] Sending request for product={product}...")
    version = random.choice(PRODUCT_VERSIONS[product])
    print(f"[{cid}] Sending request for product={version}...")


    headers = {
        "Content-Type": "application/json",
        "X-Conversation-ID": cid
    }
    payload = {
        "conversation_id": cid,
        "user_input": message,
        "product": product,
        "version": version
    }
    start = time.perf_counter()
    try:
        async with session.post(url, json=payload, headers=headers) as resp:
            await resp.text()  # consume body
            elapsed = time.perf_counter() - start
            returned_cid = resp.headers.get("X-Conversation-ID", "none")
            return resp.status, elapsed, returned_cid
    except Exception as e:
        elapsed = time.perf_counter() - start
        return f"EXC:{e.__class__.__name__}", elapsed, cid

async def run_load_test(args):
    url         = args.url
    total       = args.total
    concurrency = args.concurrent
    message     = args.message

    sem   = asyncio.Semaphore(concurrency)
    times = []
    codes = []

    async with aiohttp.ClientSession() as session:
        async def worker(i):
            async with sem:
                status, elapsed, cid = await send_chat(session, url, message)
                times.append(elapsed)
                codes.append(status)
                print(f"[{cid}] Request #{i} failed → status={status}, time={elapsed:.3f}s")

        tasks = [asyncio.create_task(worker(i)) for i in range(1, total+1)]
        await asyncio.gather(*tasks)

    # summary
    ok    = sum(1 for c in codes if c == 200)
    fail  = total - ok
    exc   = sum(1 for c in codes if isinstance(c, str) and c.startswith("EXC:"))
    p50   = statistics.median(times) if times else 0
    p90   = statistics.quantiles(times, n=10)[8] if len(times) >= 10 else max(times, default=0)

    print(f"\n—— Load Test Results ——")
    print(f"Endpoint:         {url}")
    print(f"Total requests:   {total}")
    print(f"Concurrency:      {concurrency}")
    print(f"200 OK:           {ok}")
    print(f"Failures:         {fail} (exceptions: {exc})")
    if times:
        print(f"Min latency:      {min(times):.3f}s")
        print(f"Max latency:      {max(times):.3f}s")
        print(f"Avg latency:      {statistics.mean(times):.3f}s")
        print(f"P50 latency:      {p50:.3f}s")
        print(f"P90 latency:      {p90:.3f}s")

def parse_args():
    p = argparse.ArgumentParser(description="Asyncio load-tester for /chat with random product/version")
    p.add_argument("--url",        "-u", default="http://localhost:8000/chat",
                                         help="Chat endpoint URL")
    p.add_argument("--total",      "-n", type=int, default=100,
                                         help="Total number of requests to send")
    p.add_argument("--concurrent", "-c", type=int, default=10,
                                         help="Max in-flight requests")
    p.add_argument("--message",    "-m", default="Hello, is there an update?",
                                         help="The `user_input` to send")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(run_load_test(args))
    except KeyboardInterrupt:
        sys.exit("\nAborted by user")
