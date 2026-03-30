# threshold-engine

Session-aware dynamic alerting thresholds for Prometheus and Mimir.

Instead of static alert thresholds that fire too often during busy periods and miss anomalies during quiet ones, threshold-engine trains a baseline model per metric per named session and writes dynamic bounds back into your metrics store. Alert rules compare live metrics against those bounds.

---

## How it works

1. **Define sessions** — named time windows with distinct expected behaviour (e.g. `pre_open`, `continuous`, `market_close`, `overnight`)
2. **Define metrics** — any PromQL query you want to monitor
3. **Engine trains** — for each (metric, session) pair, it pulls 90 days of history, groups data points by their relative minute within the session, and computes mean ± std for each minute
4. **Engine writes bounds** back into Mimir:
   - `te_{metric}_predicted` — historical mean at the current minute
   - `te_{metric}_upper` — mean + (multiplier × std)
   - `te_{metric}_lower` — mean - (multiplier × std)
   - `te_{metric}_anomaly_score` — z-score of the most recent actual value
   - `te_{metric}_model_ok` — 1 if trained, 0 if insufficient history
5. **Alert rules** fire when the live metric exceeds the session-aware bound

The result: high latency during an auction open doesn't page you, because the baseline captures that it's normal. The same spike during overnight would.

---

## Quickstart

### With Docker Compose

```bash
git clone https://github.com/0basement/threshold-engine
cd threshold-engine

cp example_config.yaml config.yaml
# Edit config.yaml — set your metric queries

docker-compose up --build
```

Mimir starts on `http://localhost:9009`. The engine runs immediately and repeats every `update_interval_minutes`.

Query the bounds:
```bash
curl 'http://localhost:9009/prometheus/api/v1/query?query=te_request_latency_p99_upper'
```

### Standalone (existing Mimir/Prometheus)

```bash
pip install git+https://github.com/0basement/threshold-engine
cp example_config.yaml config.yaml
# Edit config.yaml — point prometheus.url and remote_write_url at your stack
threshold-engine --config config.yaml
```

---

## Configuration

Copy `example_config.yaml` and edit it. All platform-specific knowledge lives here.

```yaml
prometheus:
  url: "http://mimir:9009/prometheus"
  remote_write_url: "http://mimir:9009/api/v1/push"
  step: "60s"
  lookback_days: 90

engine:
  update_interval_minutes: 60
  anomaly_multiplier: 1.5      # bounds width: mean ± (1.5 × std)
  min_training_points: 1000    # skip model if insufficient history
  output_metric_prefix: "te"

sessions:
  - name: continuous
    days: [Mon, Tue, Wed, Thu, Fri]
    start: "09:00"
    end: "16:30"
    timezone: "UTC"

  - name: overnight
    days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
    start: "17:30"
    end: "07:00"   # crosses midnight
    timezone: "UTC"

calendar:
  holidays:
    - "2026-01-01"
    - "2026-04-03"

metrics:
  - name: request_latency_p99
    query: 'histogram_quantile(0.99, sum(rate(request_duration_seconds_bucket[5m])) by (le))'
    unit: seconds
```

See `example_config.yaml` for a full reference including events and half-days.

---

## Alert rules

Load `alert_rules.yaml` into Grafana or Prometheus. It contains ready-made rules for bound breaches, high z-scores, and engine health:

```yaml
- alert: RequestLatencyAnomalous
  expr: |
    request_latency_p99
    >
    te_request_latency_p99_upper
    and
    te_request_latency_p99_model_ok == 1
  for: 2m
  labels:
    severity: warning
```

The `model_ok` guard ensures alerts fall back to your existing static rules while the engine is warming up.

---

## Requirements

- Python 3.11+
- Mimir or any Prometheus-compatible store that supports remote_write
- At least `min_training_points` of metric history (default: 1000 samples ≈ ~17 hours at 60s resolution)

---

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```
