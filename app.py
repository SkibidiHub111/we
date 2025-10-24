import asyncio
import time
import random
import statistics
from multiprocessing import Process, Queue
import aiohttp
from typing import List
from flask import Flask, request, render_template
from urllib.parse import urlparse
import logging

# Thiết lập logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration constants
MAX_TOTAL = 10000
MAX_PROCESSES = 5
MAX_CONCURRENCY = 200
MAX_RPS = 1000
DEFAULT_TIMEOUT = 10

# Flask app setup
app = Flask(__name__)

async def single_request(session, method, url, timeout):
    start = time.perf_counter()
    try:
        if method == "GET":
            async with session.get(url, timeout=timeout) as resp:
                await resp.read()
                return True, resp.status, time.perf_counter() - start
        else:
            async with session.post(url, timeout=timeout) as resp:
                await resp.read()
                return True, resp.status, time.perf_counter() - start
    except Exception as e:
        logger.error(f"Request error: {e}")
        return False, str(e), time.perf_counter() - start

async def run_async_worker(pid: int, urls: List[str], n: int, concurrency: int,
                          method: str, timeout: int, randomize: bool, q: Queue, delay: float):
    sem = asyncio.Semaphore(concurrency)
    lat, succ, err, st = [], 0, 0, {}
    connector = aiohttp.TCPConnector(limit=0)
    timeout_obj = aiohttp.ClientTimeout(total=None, sock_connect=timeout, sock_read=timeout)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout_obj) as session:
        async def task(i):
            nonlocal succ, err
            async with sem:
                url = random.choice(urls) if randomize else urls[i % len(urls)]
                ok, s, t = await single_request(session, method, url, timeout)
                lat.append(t)
                if ok:
                    succ += 1
                    st[s] = st.get(s, 0) + 1
                else:
                    err += 1
                    st["ERR"] = st.get("ERR", 0) + 1
                if delay:
                    await asyncio.sleep(delay)
        tasks = [asyncio.create_task(task(i)) for i in range(n)]
        for coro in asyncio.as_completed(tasks):
            await coro
    q.put({"process_id": pid, "requests": n, "success": succ, "errors": err, "statuses": st, "latencies": lat})

def worker_process(pid: int, urls: List[str], n: int, c: int, m: str, t: int, r: bool, q: Queue, d: float):
    logger.info(f"Starting worker process {pid} with {n} requests")
    asyncio.run(run_async_worker(pid, urls, n, c, m, t, r, q, d))
    logger.info(f"Worker process {pid} completed")

def percentile(data, p):
    if not data:
        return None
    data.sort()
    k = (len(data) - 1) * (p / 100)
    f, c = int(k), min(int(k) + 1, len(data) - 1)
    if f == c:
        return data[int(k)]
    d0 = data[f] * (c - k)
    d1 = data[c] * (k - f)
    return d0 + d1

def aggregate_results(res):
    tot = sum(r["requests"] for r in res)
    suc = sum(r["success"] for r in res)
    err = sum(r["errors"] for r in res)
    st, lat = {}, []
    for r in res:
        for k, v in r["statuses"].items():
            st[k] = st.get(k, 0) + v
        lat.extend(r["latencies"])
    stats = {"requests": tot}
    if lat:
        stats.update({
            "min": min(lat),
            "max": max(lat),
            "mean": statistics.mean(lat),
            "p50": statistics.median(lat),
            "p90": percentile(lat, 90),
            "p95": percentile(lat, 95)
        })
    return {"total_requests": tot, "total_success": suc, "total_errors": err, "status_breakdown": st, "latency_stats": stats}

@app.route("/", methods=["GET", "POST"])
def load_test():
    if request.method == "POST":
        url = request.form.get("url")
        mode = request.form.get("mode")

        # Validate URL
        try:
            parsed_url = urlparse(url)
            if not parsed_url.scheme or not parsed_url.netloc:
                return render_template("index.html", error="Invalid URL provided.")
            domain = parsed_url.netloc
            logger.info(f"Domain: {domain}")
        except Exception as e:
            logger.error(f"Error parsing URL: {e}")
            return render_template("index.html", error="Invalid URL format.")

        # Configure test parameters based on mode
        if mode == "normal":
            total = 500
            processes = 2
            concurrency = 50
            ramp = 10
        else:  # max
            total = 5000
            processes = 5
            concurrency = 200
            ramp = 5

        method = "GET"
        timeout = DEFAULT_TIMEOUT
        randomize = True
        urls = [url]

        # Check RPS safety cap
        estimated_rps = total / max(1, ramp if ramp > 0 else 1)
        logger.info(f"Estimated RPS: {estimated_rps}")
        if estimated_rps > MAX_RPS:
            return render_template("index.html", error=f"Estimated RPS ({estimated_rps:.1f}) exceeds safety cap ({MAX_RPS}). Reduce load or increase ramp-up.")

        # Start the test
        logger.info(f"Starting load test on {url} in {mode} mode...")
        base, rem = total // processes, total % processes
        per_proc = [base + (1 if i < rem else 0) for i in range(processes)]
        q, ps, start = Queue(), [], time.time()

        # Start worker processes
        for i in range(processes):
            p = Process(target=worker_process, args=(i + 1, urls, per_proc[i], concurrency, method, timeout, randomize, q, 0.0))
            p.start()
            ps.append(p)

        # Collect results
        res = [q.get() for _ in range(processes)]
        for p in ps:
            p.join()
        total_time = time.time() - start
        final = aggregate_results(res)
        logger.info(f"Test completed in {total_time:.3f}s")

        # Prepare results
        result = {
            "url": url,
            "mode": mode,
            "total_time": total_time,
            "total_requests": final["total_requests"],
            "total_success": final["total_success"],
            "total_errors": final["total_errors"],
            "status_breakdown": final["status_breakdown"],
            "latency_stats": final["latency_stats"],
            "throughput": final["total_requests"] / max(total_time, 1e-6)
        }

        return render_template("index.html", result=result)

    return render_template("index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
