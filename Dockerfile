# geb-all image: Debian + Godot 4.4 (headless judge + windowed record) + vendored claude-code.
# - judge containers run godot --headless, --network=none.
# - solve containers (v1) run claude-code against vLLM's Anthropic API, network on.
# - record containers run godot windowed under Xvfb + software GL (llvmpipe), then ffmpeg -> mp4.
#   (record is a separate CLI path/module/scene; it just shares this one image's toolbox.)
# Build context provides third_party/vendor/godot and third_party/packages/anthropic-ai-claude-code-linux-x64-*.tgz.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libx11-6 libxcursor1 libxinerama1 libxrandr2 libxi6 \
        libgl1 libegl1 libglu1-mesa \
        libfontconfig1 libasound2 libpulse0 \
        bash coreutils git ca-certificates \
        procps psmisc \
        xvfb xauth libgl1-mesa-dri ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Godot engine: headless (judge) + windowed-under-Xvfb+software-GL (record) both come from this one
# binary; verify both display drivers boot.
COPY third_party/vendor/godot /usr/local/bin/godot
RUN chmod +x /usr/local/bin/godot \
    && godot --headless --version \
    && xvfb-run -a -s "-screen 0 640x480x24" godot --version

# claude-code agent (vendored native binary, same approach as C2T — no npm/Node at build)
COPY third_party/packages/ /tmp/packages/
RUN set -eux; \
    native="$(find /tmp/packages -maxdepth 1 -name 'anthropic-ai-claude-code-linux-x64-*.tgz' | sort | tail -n 1)"; \
    test -n "$native" || { echo 'Missing packages/anthropic-ai-claude-code-linux-x64-<version>.tgz'; exit 2; }; \
    tar -xzf "$native" -C /tmp/packages; \
    cp /tmp/packages/package/claude /usr/local/bin/claude; \
    chmod +x /usr/local/bin/claude; \
    rm -rf /tmp/packages; \
    claude --version

# codex agent (vendored native musl binary, statically linked — second solve-phase scaffold,
# `scaffold=codex`, talks to a Responses-API endpoint). Same vendoring as claude.
COPY third_party/vendor/codex /usr/local/bin/codex
RUN chmod +x /usr/local/bin/codex && codex --version

# opencode agent (vendored native binary from github.com/anomalyco/opencode — third solve-phase
# scaffold, `scaffold=opencode`, a heterogeneous community CLI. Reaches a local model via an
# openai-compatible provider; headless run uses --pure to skip the plugin sync). Same vendoring.
COPY third_party/vendor/opencode-linux-x64 /usr/local/bin/opencode
RUN chmod +x /usr/local/bin/opencode && opencode --version

WORKDIR /workspace
CMD ["bash"]
