import asyncio
import time
import random
import statistics
from multiprocessing import Process, Queue
import aiohttp
from typing import List
from flask import Flask, request, render_template_string
from urllib.parse import urlparse
import logging

# Thiết lập logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration constants
MAX_TOTAL = 10000
MAX_PROCESSES = 10  
MAX_CONCURRENCY = 300 
MAX_RPS = 10000  
DEFAULT_TIMEOUT = 10

# Flask app setup
app = Flask(__name__)

# HTML template
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Load Test Web</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f9; }
        .container { max-width: 800px; margin: auto; padding: 20px; background: white; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }
        h1 { color: #333; }
        form { margin-bottom: 20px; }
        label { display: block; margin: 10px 0 5px; }
        input, select, button { padding: 8px; margin-bottom: 10px; width: 100%; border-radius: 4px; border: 1px solid #ccc; }
        button { background-color: #007bff; color: white; border: none; cursor: pointer; }
        button:hover { background-color: #0056b3; }
        .result { margin-top: 20px; padding: 10px; border: 1px solid #ddd; border-radius: 4px; }
        .error { color: red; }
        pre { background: #f8f8f8; padding: 10px; border-radius: 4px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>No1 Hub</h1>
        <form method="POST" action="/">
            <label for="url">URL to Test:</label>
            <input type="text" id="url" name="url" placeholder="https://example.com" required>
            <label for="mode">Test Mode:</label>
            <select id="mode" name="mode">
                <option value="normal">Normal</option>
                <option value="max">Max (Super Strong)</option>
            </select>
            <button type="submit">Run Load Test</button>
        </form>
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        {% if result %}
        <div class="result">
            <h2>Results</h2>
            <p><strong>URL:</strong> {{ result.url }}</p>
            <p><strong>Mode:</strong> {{ result.mode|capitalize }}</p>
            <p><strong>Total Runtime:</strong> {{ "%.3f"|format(result.total_time) }}s</p>
            <p><strong>Total Requests:</strong> {{ result.total_requests }}</p>
            <p><strong>Successful:</strong> {{ result.total_success }}</p>
            <p><strong>Errors:</strong> {{ result.total_errors }}</p>
            <p><strong>Status Breakdown:</strong></p>
            <pre>{{ result.status_breakdown }}</pre>
            {% if result.latency_stats.mean %}
            <p><strong>Latency Stats:</strong></p>
            <pre>
Min: {{ "%.4f"|format(result.latency_stats.min) }}s
Mean: {{ "%.4f"|format(result.latency_stats.mean) }}s
P50: {{ "%.4f"|format(result.latency_stats.p50) }}s
P90: {{ "%.4f"|format(result.latency_stats.p90) }}s
P95: {{ "%.4f"|format(result.latency_stats.p95) }}s
Max: {{ "%.4f"|format(result.latency_stats.max) }}s
            </pre>
            {% endif %}
            <p><strong>Throughput:</strong> {{ "%.2f"|format(result.throughput) }} req/s</p>
        </div>
        {% endif %}
    </div>
</body>
</html>
"""

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
                return render_template_string(HTML_TEMPLATE, error="Invalid URL provided.")
            domain = parsed_url.netloc
            logger.info(f"Domain: {domain}")
        except Exception as e:
            logger.error(f"Error parsing URL: {e}")
            return render_template_string(HTML_TEMPLATE, error="Invalid URL format.")

        # Configure test parameters based on mode
        if mode == "normal":
            total = 500
            processes = 2
            concurrency = 50
            ramp = 10
        else:  # max (super strong)
            total = 10000  # Tăng để spam mạnh hơn
            processes = 10  # Tăng để spam mạnh hơn
            concurrency = 300  # Tăng để spam mạnh hơn
            ramp = 1  # Giảm để tăng RPS nhanh chóng

        method = "GET"
        timeout = DEFAULT_TIMEOUT
        randomize = True
        urls = [url]

        # Check RPS safety cap
        estimated_rps = total / max(1, ramp if ramp > 0 else 1)
        logger.info(f"Estimated RPS: {estimated_rps}")
        if estimated_rps > MAX_RPS:
            return render_template_string(HTML_TEMPLATE, error=f"Estimated RPS ({estimated_rps:.1f}) exceeds safety cap ({MAX_RPS}). Reduce load or increase ramp-up.")

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

        return render_template_string(HTML_TEMPLATE, result=result)

    return render_template_string(HTML_TEMPLATE)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
