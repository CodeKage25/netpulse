# NetPulse, as the hosted half of a split install.
#
# What runs in here does no measuring. It cannot: the router it would poll is on a
# private address inside somebody's house, and this is a datacentre. It accepts what an
# agent at home pushes, keeps the history, and serves the dashboard.
#
# Zero dependencies, so this is the base image, the source, and nothing else — no
# compiler, no package index at build time, no lockfile to drift.

FROM python:3.13-slim

# Not root. Nothing in here needs to write outside the data volume, and a web process
# that could is one bug away from being a much larger problem.
RUN useradd --create-home --uid 10001 netpulse

WORKDIR /app
COPY pyproject.toml README.md ./
COPY netpulse ./netpulse
RUN pip install --no-cache-dir . && chown -R netpulse:netpulse /app

# The volume mount point. SQLite on a Fly volume means exactly one writer, which is why
# fly.toml pins this to a single machine.
RUN mkdir -p /data && chown netpulse:netpulse /data
VOLUME /data
USER netpulse

ENV NETPULSE_DB=/data/history.db \
    PORT=8080 \
    BIND=0.0.0.0

EXPOSE 8080

# `serve` refuses to bind anything but loopback without NETPULSE_DASHBOARD_TOKEN, so a
# container started without one exits immediately and says why. That is deliberate: the
# alternative is a public URL over somebody's router with no password on it.
CMD ["netpulse", "run"]
