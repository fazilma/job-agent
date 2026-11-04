# Job agent: dashboard + daily run + Claude Code + LaTeX, in one container.
# Build and start with:  docker compose up -d --build
FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Dubai \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1

# Python, poppler (pdfinfo/pdftotext/pdffonts) and the TeX packages the resume uses.
# texlive-fonts-extra is large (~1 GB) but it's where fontawesome5 lives.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv ca-certificates tzdata git ripgrep procps \
        poppler-utils \
        texlive-xetex texlive-latex-recommended texlive-latex-extra \
        texlive-fonts-recommended texlive-fonts-extra texlive-lang-english \
        fonts-lmodern lmodern \
    && rm -rf /var/lib/apt/lists/*

# Claude Code. Installed from /tmp: installing from / can hang in Docker (per the docs).
WORKDIR /tmp
RUN npm install -g @anthropic-ai/claude-code && claude --version

# Fail the build now if the resume preamble can't compile.
COPY docker/texcheck.tex /tmp/texcheck/
RUN cd /tmp/texcheck \
    && xelatex -interaction=nonstopmode -halt-on-error texcheck.tex > /dev/null \
    && pdfinfo texcheck.pdf | grep -q "Pages: *1" \
    && cd / && rm -rf /tmp/texcheck

# The app itself (editable install, so the project root is /app).
WORKDIR /app
COPY pyproject.toml ./
COPY jobagent ./jobagent
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -e .
ENV PATH=/opt/venv/bin:$PATH

# Agent Reach, for the web_search source. Pinned to the project's release: the PyPI package with
# this name is a different project. To upgrade, change the version and its sha256.
RUN python3 -m venv /opt/reach \
    && /opt/reach/bin/pip install --no-cache-dir \
       "https://github.com/Panniantong/Agent-Reach/releases/download/v1.6.1/agent-reach.zip#sha256=51443d733bcb26064262442083a8f8abf6ba5e3e7df54beebafe98c88be04a60" \
    && ln -s /opt/reach/bin/agent-reach /usr/local/bin/agent-reach

# Resume skills, as local skills (docker-compose also mounts docker/skills over this,
# so ./docker/update-skills.sh takes effect without a rebuild).
COPY docker/skills /root/.claude/skills

EXPOSE 8765
HEALTHCHECK --interval=5m --timeout=10s --start-period=30s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=5)"

# Dashboard on 0.0.0.0 (docker-compose publishes it to 127.0.0.1 on the Mac only),
# plus the daily run at JOBAGENT_DAILY_AT.
CMD ["jobagent", "serve", "--host", "0.0.0.0"]
