(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const messagesEl = $("#messages");
  const composer = $("#composer");
  const promptEl = $("#prompt");
  const sendButton = $("#sendButton");
  const welcome = $("#welcome");
  const toast = $("#toast");
  const sidebar = document.querySelector(".sidebar");

  const state = {
    sessionId: localStorage.getItem("spark_session_id") || randomId(),
    messages: [],
    plan: [],
    conversations: [],
    settings: {},
    health: {},
    running: false,
    loopStep: 0,
    attachments: [],
    controller: null,
    toastTimer: null,
    renderPending: false,
  };

  localStorage.setItem("spark_session_id", state.sessionId);

  function randomId() {
    const bytes = new Uint8Array(12);
    crypto.getRandomValues(bytes);
    return "web-" + Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function inlineMarkdown(value) {
    let text = escapeHtml(value);
    // Closed constructs first, then unclosed tails so streaming can paint from the left.
    text = text.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    text = text.replace(/\*\*([^*\n]+?)\*\*/g, "<strong>$1</strong>");
    // Unclosed ** at end of the current line: still open a <strong> to the end.
    text = text.replace(/\*\*([^*\n]+)$/g, "<strong>$1</strong>");
    text = text.replace(/(?<!\*)\*([^*\n]+?)\*(?!\*)/g, "<em>$1</em>");
    // Unclosed ` at end of line
    text = text.replace(/`([^`\n]+)$/g, "<code>$1</code>");
    text = text.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    return text;
  }

  function splitMarkdownRow(line) {
    let row = String(line).trim();
    if (row.startsWith("|")) row = row.slice(1);
    if (row.endsWith("|")) row = row.slice(0, -1);
    return row.split("|").map(cell => cell.trim());
  }

  function isMarkdownSeparator(line) {
    const cells = splitMarkdownRow(line);
    return cells.length > 1 && cells.every(cell => /^:?-{3,}:?$/.test(cell));
  }

  function renderMarkdownTable(lines, startIndex) {
    const header = splitMarkdownRow(lines[startIndex]);
    let index = startIndex + 1;
    if (!isMarkdownSeparator(lines[index] || "")) return null;
    index += 1;
    const rows = [];
    while (index < lines.length && lines[index].trim().includes("|")) {
      rows.push(splitMarkdownRow(lines[index]));
      index += 1;
    }
    const head = header.map(cell => `<th>${inlineMarkdown(cell)}</th>`).join("");
    const body = rows.map(row => {
      const cells = header.map((_, cellIndex) => `<td>${inlineMarkdown(row[cellIndex] || "")}</td>`).join("");
      return `<tr>${cells}</tr>`;
    }).join("");
    return {
      html: `<div class="md-table-wrap"><table class="md-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`,
      nextIndex: index - 1,
    };
  }

  /**
   * Models often emit "intro： - a - b - c" or "text ## 标题 1. item" on one line.
   * Also common: "1、" "1）" "1." without a space, and blank lines between numbers.
   * Insert newlines so the block parser can see real list/heading markers.
   */
  function normalizeBlockMarkdown(value) {
    let text = String(value || "");
    // Mid-line ATX heading: "text ## 标题"
    text = text.replace(/([^\n])\s+(#{1,6}\s+)/g, "$1\n$2");
    // Mid-line ordered item: "text 1. 内容" / "1、" / "1）" (not 3.14 / v1.2)
    text = text.replace(/([^\n])\s+(\d{1,2}[\.、．)】]\s*)/g, "$1\n$2");
    // Mid-line unordered item: "： - 内容" or "a - b - c"
    text = text.replace(/\s+([-*])\s+/g, "\n$1 ");
    // Glued follow-up sentence after a list item: "较小 整体来看，…"
    text = text.replace(
      /([^\n]+?)\s+(?=[\u4e00-\u9fff]{0,3}(?:整体来看|整体|另外|总之|综上|最后)[\u4e00-\u9fff，。；、])/g,
      "$1\n",
    );
    return text;
  }

  function isOrderedLine(line) {
    return /^\s*\d{1,2}[\.、．)】]\s*\S/.test(line);
  }

  function isUnorderedLine(line) {
    return /^\s*[-*]\s+\S/.test(line);
  }

  function textBlock(value) {
    const lines = normalizeBlockMarkdown(value).split("\n");
    const out = [];
    let list = null;
    const closeList = () => {
      if (list) {
        out.push(`</${list}>`);
        list = null;
      }
    };

    for (let index = 0; index < lines.length; index += 1) {
      const raw = lines[index];
      const line = raw.trimEnd();
      const table = line.includes("|") ? renderMarkdownTable(lines, index) : null;
      if (table) {
        closeList();
        out.push(table.html);
        index = table.nextIndex;
        continue;
      }
      // ATX heading: #标题 / # 标题 / ### (empty). Level 1–6, text optional.
      const heading = line.match(/^(#{1,6})(.*)$/);
      const ordered = line.match(/^\s*(\d{1,2})[\.、．)】]\s*(.+)$/);
      const unordered = line.match(/^\s*[-*]\s+(.+)$/);
      const quote = line.match(/^>\s?(.*)$/);

      if (heading) {
        closeList();
        const level = heading[1].length;
        const headingText = String(heading[2] || "").trim();
        // Skip lone hashes that are not headings (e.g. inside code we never get here).
        if (headingText || /^[#]+$/.test(line.trim())) {
          out.push(`<h${level}>${inlineMarkdown(headingText)}</h${level}>`);
        } else {
          out.push(`<p>${inlineMarkdown(line)}</p>`);
        }
      } else if (unordered) {
        if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
        out.push(`<li>${inlineMarkdown(unordered[1])}</li>`);
      } else if (ordered) {
        // One shared <ol> so the browser renumbers even when every item is "1."
        if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
        out.push(`<li>${inlineMarkdown(ordered[2])}</li>`);
      } else if (quote) {
        closeList();
        out.push(`<blockquote>${inlineMarkdown(quote[1])}</blockquote>`);
      } else if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        closeList();
        out.push("<hr>");
      } else if (line.trim() === "") {
        // Blank line between numbered items must NOT split the <ol>
        // (each tiny <ol> would restart at 1).
        let next = index + 1;
        while (next < lines.length && !lines[next].trim()) next += 1;
        const peek = lines[next] || "";
        if (list === "ol" && isOrderedLine(peek)) continue;
        if (list === "ul" && isUnorderedLine(peek)) continue;
        closeList();
      } else {
        closeList();
        out.push(`<p>${inlineMarkdown(line)}</p>`);
      }
    }
    closeList();
    return out.join("");
  }

  function renderMarkdown(value) {
    const raw = String(value || "");
    const segments = raw.split(/```/);
    let html = "";
    segments.forEach((segment, index) => {
      if (index % 2 === 1) {
        // Odd segment = inside a fence. Unclosed fence (still streaming) still
        // renders as a code block from ```json onward.
        const firstBreak = segment.indexOf("\n");
        const lang = firstBreak >= 0 ? segment.slice(0, firstBreak).trim() : "";
        const code = firstBreak >= 0 ? segment.slice(firstBreak + 1) : segment;
        html += `<pre data-lang="${escapeHtml(lang)}"><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`;
      } else if (segment) {
        html += textBlock(segment);
      }
    });
    return html;
  }

  function showToast(message) {
    toast.textContent = message;
    toast.classList.add("show");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toast.classList.remove("show"), 5000);
  }

  function createMessage(role, content = "", extra = {}) {
    if (welcome) welcome.style.display = "none";
    const root = document.createElement("article");
    root.className = `message ${role}`;
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.setAttribute("aria-hidden", "true");
    if (role === "user") {
      avatar.textContent = "你";
    } else {
      const mark = document.createElement("span");
      mark.textContent = "S";
      avatar.appendChild(mark);
    }
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    const body = document.createElement("div");
    body.className = "message-content";
    body.innerHTML = renderMarkdown(content);
    bubble.appendChild(body);
    root.append(avatar, bubble);
    messagesEl.appendChild(root);
    scrollBottom(true);

    if (role === "assistant") {
      const reasoning = document.createElement("details");
      reasoning.className = "reasoning";
      reasoning.style.display = "none";
      reasoning.innerHTML =
        "<summary class='reasoning-summary'><span class='reasoning-tag'>思考</span></summary>" +
        "<div class='reasoning-body'></div>";
      bubble.insertBefore(reasoning, body);

      const tools = document.createElement("div");
      tools.className = "tool-events";
      bubble.insertBefore(tools, body);

      if (Array.isArray(extra.blocks) && extra.blocks.length) {
        // History messages keep reasoning/text/tool blocks in their original order.
        reasoning.remove();
        tools.remove();
        body.innerHTML = "";
        for (const block of extra.blocks) {
          if (block.type === "reasoning") {
            body.appendChild(makeReasoningNode("思考", block.content || ""));
          } else if (block.type === "later_reasoning") {
            body.appendChild(makeReasoningNode("后续轮次思考", block.content || ""));
          } else if (block.type === "tool") {
            renderTool(body, block);
          } else if (block.type === "text" && block.content) {
            const textBlock = document.createElement("div");
            textBlock.className = "saved-text-block";
            textBlock.innerHTML = renderMarkdown(block.content);
            body.appendChild(textBlock);
          }
        }
      } else {
        if (extra.reasoning) {
          reasoning.style.display = "";
          reasoning.querySelector(".reasoning-body").innerHTML = renderMarkdown(extra.reasoning);
        }
        if (extra.tools?.length) {
          for (const tool of extra.tools) renderTool(tools, tool);
        }
      }
    }
    return { root, bubble, body, reasoning: bubble.querySelector(".reasoning"), tools: bubble.querySelector(".tool-events") };
  }

  /** Collapsible thinking block (per round) — visually distinct from the answer. */
  function makeReasoningNode(label, html, open = false) {
    const details = document.createElement("details");
    details.className = "reasoning";
    details.open = open;
    const summary = document.createElement("summary");
    summary.className = "reasoning-summary";
    const tag = document.createElement("span");
    tag.className = "reasoning-tag";
    tag.textContent = label || "思考";
    const hint = document.createElement("span");
    hint.className = "reasoning-hint";
    hint.textContent = open ? "点击折叠" : "点击展开";
    summary.append(tag, hint);
    const bodyEl = document.createElement("div");
    bodyEl.className = "reasoning-body";
    bodyEl.innerHTML = html || "";
    details.append(summary, bodyEl);
    details.addEventListener("toggle", () => {
      hint.textContent = details.open ? "点击折叠" : "点击展开";
    });
    return details;
  }

  function setReasoningOpen(details, open) {
    if (!details) return;
    if (details.open === Boolean(open)) return;
    details.open = Boolean(open);
    const hint = details.querySelector(".reasoning-hint");
    if (hint) hint.textContent = details.open ? "点击折叠" : "点击展开";
  }

  function renderSavedMessages() {
    messagesEl.innerHTML = "";
    if (!state.messages.length) {
      messagesEl.appendChild(welcome);
      welcome.style.display = "";
      return;
    }
    for (const message of state.messages) {
      createMessage(message.role || "assistant", message.content || "", message);
    }
  }

  function previewArgs(args) {
    if (args == null) return "";
    const value = typeof args === "string" ? args : JSON.stringify(args);
    return value.replace(/\s+/g, " ").slice(0, 130);
  }

  const TOOL_ICONS = {
    shell: ">_",
    read: "📄",
    grep: "⌕",
    ls: "☰",
    del: "🗑",
    write: "✎",
    edit: "✂",
    diff: "±",
    plan: "☑",
    askuser: "💬",
    web_search: "🌐",
    compact_context: "压缩",
    analyze: "📊",
    show: "▶",
    office: "表格",
    rename: "↔",
    organize: "▦",
  };

  function toolIcon(name) {
    return TOOL_ICONS[name] || "◆";
  }

  function addArtifactButton(container, path) {
    if (!path) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "artifact-open";
    button.textContent = "↗ 打开";
    button.title = `打开 ${path}`;
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "打开中…";
      try {
        const response = await fetch("/api/open", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        showToast("已请求系统打开产物");
      } catch (error) {
        showToast("打开失败：" + (error.message || "未知错误"));
      } finally {
        button.disabled = false;
        button.textContent = "↗ 打开";
      }
    });
    container.appendChild(button);
  }

  function renderDiff(container, diff) {
    if (!diff || diff === "无差异。") return;
    const view = document.createElement("div");
    view.className = "diff-view";
    for (const line of String(diff).split("\n")) {
      const row = document.createElement("div");
      let className = "diff-line context";
      if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@")) className = "diff-line header";
      else if (line.startsWith("+")) className = "diff-line add";
      else if (line.startsWith("-")) className = "diff-line remove";
      row.className = className;
      row.textContent = line || " ";
      view.appendChild(row);
    }
    container.appendChild(view);
  }

  function renderFileEntries(container, entries) {
    if (!Array.isArray(entries) || !entries.length) return;
    const list = document.createElement("div");
    list.className = "file-list";
    for (const entry of entries.slice(0, 100)) {
      const row = document.createElement("div");
      row.className = "file-row";
      const icon = document.createElement("span");
      icon.className = "file-icon";
      icon.textContent = entry.type === "dir" ? "📁" : "📄";
      const name = document.createElement("span");
      name.className = "file-name";
      name.textContent = entry.name;
      const meta = document.createElement("span");
      meta.className = "file-meta";
      meta.textContent = entry.type === "dir"
        ? "目录"
        : `${formatBytes(Number(entry.size) || 0)}`;
      row.append(icon, name, meta);
      if (entry.type === "file") addArtifactButton(row, entry.path);
      list.appendChild(row);
    }
    container.appendChild(list);
  }

  function renderSearchResults(container, results) {
    const list = document.createElement("div");
    list.className = "search-results";
    results.slice(0, 10).forEach(item => {
      const row = document.createElement("details");
      row.className = "search-result";
      const summary = document.createElement("summary");
      summary.textContent = item.title || item.url || "搜索结果";
      const body = document.createElement("div");
      body.className = "search-result-body";
      if (item.snippet) {
        const snippet = document.createElement("div");
        snippet.className = "search-snippet";
        snippet.textContent = item.snippet;
        body.appendChild(snippet);
      }
      if (item.url) {
        const link = document.createElement("a");
        link.href = item.url;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = item.url;
        body.appendChild(link);
      }
      row.append(summary, body);
      list.appendChild(row);
    });
    container.appendChild(list);
  }

  function renderTool(container, tool) {
    let card = container.querySelector(`[data-tool-id="${CSS.escape(String(tool.id || ""))}"]`);
    if (!card) {
      card = document.createElement("details");
      card.className = "tool-card";
      card.dataset.toolId = String(tool.id || "");
      card.innerHTML = `
        <summary>
          <span class="tool-icon">${escapeHtml(toolIcon(tool.name))}</span>
          <span class="tool-name">${escapeHtml(tool.name || "tool")}</span>
          <span class="tool-preview"></span>
          <span class="tool-status">运行中</span>
        </summary>
        <div class="tool-detail"></div>`;
      container.appendChild(card);
    }
    // Preview line: short human summary of args
    const previewEl = card.querySelector(".tool-preview");
    previewEl.textContent = toolPreviewLine(tool);
    const status = card.querySelector(".tool-status");
    const detail = card.querySelector(".tool-detail");
    const result = tool.result;
    const objectResult = result && typeof result === "object" && !Array.isArray(result);
    const failed = tool.outcome && tool.outcome !== "success";

    detail.innerHTML = "";

    // ---- 输入：结构化 kv chips + 可折叠原始 JSON ----
    const inputBlock = document.createElement("div");
    inputBlock.className = "tool-io-card input-card";
    const inputLabel = document.createElement("div");
    inputLabel.className = "io-label";
    inputLabel.innerHTML = `<span>输入</span><button type="button" class="io-toggle" data-io-raw>JSON</button>`;
    const inputKv = document.createElement("div");
    inputKv.className = "io-kv";
    const argsObj = typeof tool.args === "string"
      ? (() => { try { return JSON.parse(tool.args); } catch { return null; } })()
      : (tool.args || null);
    if (argsObj && typeof argsObj === "object" && !Array.isArray(argsObj)) {
      const entries = Object.entries(argsObj).filter(([, v]) => v !== undefined && v !== null && v !== "");
      if (!entries.length) {
        inputKv.innerHTML = `<div class="io-empty">无参数</div>`;
      } else {
        for (const [k, v] of entries) {
          const row = document.createElement("div");
          row.className = "io-kv-row";
          const key = document.createElement("span");
          key.className = "io-k";
          key.textContent = k;
          const val = document.createElement("span");
          val.className = "io-v";
          val.textContent = typeof v === "object" ? JSON.stringify(v) : String(v);
          row.append(key, val);
          inputKv.appendChild(row);
        }
      }
    } else {
      inputKv.className = "io-kv io-kv-raw";
      const pre = document.createElement("pre");
      pre.className = "io-pre";
      pre.textContent = typeof tool.args === "string" ? tool.args : JSON.stringify(tool.args, null, 2);
      inputKv.appendChild(pre);
    }
    const inputRaw = document.createElement("pre");
    inputRaw.className = "io-pre io-raw";
    inputRaw.hidden = true;
    inputRaw.textContent = typeof tool.args === "string"
      ? tool.args
      : JSON.stringify(tool.args, null, 2);
    inputBlock.append(inputLabel, inputKv, inputRaw);
    inputBlock.querySelector("[data-io-raw]")?.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const showRaw = inputRaw.hidden;
      inputRaw.hidden = !showRaw;
      inputKv.hidden = showRaw;
      e.currentTarget.classList.toggle("on", showRaw);
      e.currentTarget.textContent = showRaw ? "表单" : "JSON";
    });
    detail.appendChild(inputBlock);

    if (result == null) {
      status.textContent = "运行中";
      status.className = "tool-status";
      return card;
    }

    status.textContent = failed ? "失败" : "完成";
    status.className = `tool-status ${failed ? "error" : "ok"}`;
    const outputBlock = document.createElement("div");
    outputBlock.className = "tool-io-card output-card";
    const outputLabel = document.createElement("div");
    outputLabel.className = "io-label";
    outputLabel.textContent = "输出";
    outputBlock.appendChild(outputLabel);

    let message;
    if (objectResult && (tool.name === "show" || tool.name === "office")) {
      message = objectResult.message || objectResult.error || objectResult.caption || "";
    } else if (objectResult) {
      message = objectResult.message || objectResult.error ||
        (Array.isArray(objectResult.results)
          ? `${objectResult.results.length} 条搜索结果 · ${objectResult.engine || "web"}`
          : JSON.stringify(objectResult, null, 2));
    } else {
      message = String(result);
    }
    if (message) {
      const messageBlock = document.createElement("div");
      messageBlock.className = "tool-message";
      messageBlock.textContent = message;
      outputBlock.appendChild(messageBlock);
    }
    if (objectResult?.diff) renderDiff(outputBlock, objectResult.diff);
    if (objectResult?.preview?.length) {
      const preview = document.createElement("div");
      preview.className = "preview-list";
      for (const item of objectResult.preview) {
        const row = document.createElement("div");
        row.textContent = `${item.from}  →  ${item.to}`;
        preview.appendChild(row);
      }
      outputBlock.appendChild(preview);
    }
    if (objectResult?.entries) renderFileEntries(outputBlock, objectResult.entries);
    if (tool.name === "web_search" && Array.isArray(objectResult?.results)) {
      renderSearchResults(outputBlock, objectResult.results);
    }
    // office read → table card
    if (tool.name === "office" && objectResult?.ok) {
      if (objectResult.kind === "table" && objectResult.headers) {
        renderShow(outputBlock, {
          kind: "table",
          headers: objectResult.headers,
          rows: objectResult.rows,
          row_dicts: objectResult.row_dicts,
          title: objectResult.title || objectResult.path || "表格",
          caption: objectResult.message || "",
        });
      } else if (objectResult.kind === "document" && objectResult.text) {
        renderShow(outputBlock, {
          kind: "text",
          text: objectResult.text,
          title: objectResult.title || objectResult.path || "文档",
          caption: objectResult.message || "",
        });
      } else if (objectResult.kind === "spreadsheet" && objectResult.preview) {
        const firstSheet = Object.keys(objectResult.preview)[0];
        if (firstSheet) {
          const matrix = objectResult.preview[firstSheet] || [];
          const headers = (matrix[0] || []).map(String);
          const rows = matrix.slice(1);
          renderShow(outputBlock, {
            kind: "table",
            headers,
            rows,
            title: `${objectResult.path || ""} · ${firstSheet}`,
            caption: objectResult.message || "",
          });
        }
      }
    }
    if (tool.name === "show" && objectResult?.ok) {
      renderShow(outputBlock, objectResult);
    }

    const artifactPaths = objectResult
      ? (Array.isArray(objectResult.paths) ? objectResult.paths : (objectResult.path ? [objectResult.path] : []))
      : [];
    if (["write", "edit", "rename", "organize", "analyze", "ls", "show", "office"].includes(tool.name) && artifactPaths.length) {
      const artifacts = document.createElement("div");
      artifacts.className = "artifact-row";
      const label = document.createElement("span");
      label.textContent = "产物";
      artifacts.appendChild(label);
      for (const path of artifactPaths.slice(0, 12)) addArtifactButton(artifacts, path);
      outputBlock.appendChild(artifacts);
    }
    detail.appendChild(outputBlock);
    card.open = Boolean(
      objectResult?.kind === "image"
      || objectResult?.kind === "url"
      || objectResult?.kind === "table"
      || objectResult?.kind === "mindmap"
      || (objectResult?.kind === "text" && String(objectResult.text || "").length > 0)
      || (tool.name === "office" && objectResult?.kind === "table")
    );
    scrollBottom(true);
    return card;
  }

  function toolPreviewLine(tool) {
    const args = typeof tool.args === "string"
      ? (() => { try { return JSON.parse(tool.args); } catch { return tool.args; } })()
      : tool.args;
    if (!args || typeof args !== "object") return String(args || "").replace(/\s+/g, " ").slice(0, 80);
    if (tool.name === "show") return `${args.kind || ""} ${args.title || args.target?.slice?.(0, 40) || ""}`.trim();
    if (tool.name === "office") return `${args.action || "inspect"} ${args.path || ""}`.trim();
    if (tool.name === "web_search") return String(args.query || "");
    if (args.path) return String(args.path);
    if (args.pattern) return String(args.pattern);
    if (args.command) return String(args.command).slice(0, 60);
    if (args.question) return String(args.question).slice(0, 60);
    const vals = Object.values(args).filter(v => typeof v === "string" || typeof v === "number");
    return vals.map(String).join(" · ").slice(0, 80);
  }

  /** Preview pane for show / office (image / url / table / mindmap / text). */
  function renderShow(container, data) {
    const card = document.createElement("div");
    card.className = "show-card";
    const title = document.createElement("div");
    title.className = "show-title";
    title.textContent = data.title || "展示";
    card.appendChild(title);

    if (data.kind === "image" && data.src) {
      const img = document.createElement("img");
      img.className = "show-image";
      img.src = data.src;
      img.alt = data.title || "image";
      img.loading = "lazy";
      img.addEventListener("error", () => {
        img.replaceWith(Object.assign(document.createElement("div"), {
          className: "show-fallback",
          textContent: `图片加载失败：${data.src}`,
        }));
      });
      card.appendChild(img);
    } else if (data.kind === "url" && data.url) {
      // Clickable link card — open in new tab (iframe often blocked by X-Frame-Options).
      const linkCard = document.createElement("a");
      linkCard.className = "show-link-card";
      linkCard.href = data.url;
      linkCard.target = "_blank";
      linkCard.rel = "noreferrer";
      const host = document.createElement("div");
      host.className = "show-link-host";
      host.textContent = data.host || (() => { try { return new URL(data.url).host; } catch { return data.url; } })();
      const path = document.createElement("div");
      path.className = "show-link-path";
      try {
        const u = new URL(data.url);
        path.textContent = (u.pathname || "/") + (u.search || "");
      } catch { path.textContent = data.url; }
      const cta = document.createElement("div");
      cta.className = "show-link-cta";
      cta.textContent = "打开网页 ↗";
      linkCard.append(host, path, cta);
      card.appendChild(linkCard);
      const open = document.createElement("a");
      open.className = "show-open";
      open.href = data.url;
      open.target = "_blank";
      open.rel = "noreferrer";
      open.textContent = "在新标签打开 ↗";
      card.appendChild(open);
    } else if (data.kind === "table" && data.headers) {
      const wrap = document.createElement("div");
      wrap.className = "show-table-wrap";
      const table = document.createElement("table");
      table.className = "show-table";
      const thead = document.createElement("thead");
      const trh = document.createElement("tr");
      for (const h of data.headers) {
        const th = document.createElement("th");
        th.textContent = String(h);
        trh.appendChild(th);
      }
      thead.appendChild(trh);
      table.appendChild(thead);
      const tbody = document.createElement("tbody");
      const rows = data.rows || [];
      for (const row of rows.slice(0, 80)) {
        const tr = document.createElement("tr");
        for (const cell of row) {
          const td = document.createElement("td");
          td.textContent = cell == null ? "" : String(cell);
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
      wrap.appendChild(table);
      card.appendChild(wrap);
      if (data.path) {
        const openBtn = document.createElement("button");
        openBtn.type = "button";
        openBtn.className = "artifact-open";
        openBtn.textContent = "↗ 打开文件";
        openBtn.addEventListener("click", async () => {
          openBtn.disabled = true;
          try {
            await fetch("/api/open", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ path: data.path }),
            });
            showToast("已请求系统打开");
          } catch (e) {
            showToast("打开失败：" + e.message);
          } finally {
            openBtn.disabled = false;
          }
        });
        card.appendChild(openBtn);
      }
    } else if (data.kind === "mindmap" && data.tree) {
      card.appendChild(buildMindmap(data.tree));
    } else if (data.kind === "mermaid" && data.code) {
      const pre = document.createElement("pre");
      pre.className = "show-text";
      pre.textContent = data.code;
      card.appendChild(pre);
      const hint = document.createElement("div");
      hint.className = "show-caption";
      hint.textContent = "Mermaid 源码（可复制到支持 Mermaid 的编辑器）";
      card.appendChild(hint);
    } else if (data.kind === "text" && data.text) {
      const pre = document.createElement("pre");
      pre.className = "show-text";
      pre.textContent = data.text;
      card.appendChild(pre);
    }

    if (data.caption) {
      const cap = document.createElement("div");
      cap.className = "show-caption";
      cap.textContent = data.caption;
      card.appendChild(cap);
    }
    container.appendChild(card);
  }

  function buildMindmap(tree) {
    const root = document.createElement("div");
    root.className = "mindmap";
    function branch(node, depth) {
      const item = document.createElement("div");
      item.className = "mindmap-node";
      item.style.setProperty("--d", String(depth));
      const label = document.createElement("button");
      label.type = "button";
      label.className = "mindmap-label";
      label.textContent = node.label || "节点";
      item.appendChild(label);
      const kids = Array.isArray(node.children) ? node.children : [];
      if (kids.length) {
        const childBox = document.createElement("div");
        childBox.className = "mindmap-children";
        for (const k of kids) childBox.appendChild(branch(k, depth + 1));
        item.appendChild(childBox);
      }
      return item;
    }
    root.appendChild(branch(tree, 0));
    return root;
  }

  function scrollBottom(force = false) {
    const el = messagesEl;
    // Double rAF: one frame for DOM write, one for layout, so scrollHeight is final.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
        // Pin while streaming; otherwise only follow if already near the bottom
        // (so reading history doesn't get yanked down).
        if (force || state.running || gap < 120) {
          el.scrollTop = el.scrollHeight;
        }
      });
    });
  }

  function scheduleRender(element, fullText) {
    if (state.renderPending) return;
    state.renderPending = true;
    requestAnimationFrame(() => {
      element.body.innerHTML = renderMarkdown(fullText);
      state.renderPending = false;
      scrollBottom();
    });
  }

  function makeMistChunk(text) {
    const span = document.createElement("span");
    span.className = "mist-chunk";
    span.textContent = text;
    // Pure CSS animation on insert (fill: both) — survives token-by-token
    // innerHTML replaces; JS transitions + rAF never get a chance to run.
    const settle = () => {
      const node = document.createTextNode(span.textContent || "");
      const parent = span.parentNode;
      if (!parent) return;
      span.replaceWith(node);
      parent.normalize();
    };
    span.addEventListener("animationend", settle, { once: true });
    setTimeout(() => {
      if (span.isConnected) settle();
    }, 700);
    return span;
  }

  /** Append point at the end of the last block element (no full re-render). */
  function getStreamTail(root) {
    // Only reuse a tail that is truly at the end (never an orphan from an earlier para).
    const tails = root.querySelectorAll(".stream-tail");
    let existing = null;
    for (const t of tails) {
      if (!t.nextElementSibling && (!t.parentElement || !t.parentElement.nextElementSibling)) {
        existing = t;
      }
    }
    if (existing) return existing;

    const tail = document.createElement("span");
    tail.className = "stream-tail";
    const last = root.lastElementChild;
    if (!last) {
      root.appendChild(tail);
      return tail;
    }
    // Streaming inside a code fence: must land inside <pre><code>, not after <pre>.
    if (last.tagName === "PRE") {
      const code = last.querySelector("code") || last;
      const codeTail = code.querySelector(".stream-tail");
      if (codeTail) return codeTail;
      code.appendChild(tail);
      return tail;
    }
    if (/^(P|DIV|LI|H[1-6]|BLOCKQUOTE|TD|TH|CODE|EM|STRONG|A)$/.test(last.tagName)) {
      last.appendChild(tail);
    } else {
      last.parentNode.insertBefore(tail, last.nextSibling);
    }
    return tail;
  }

  /** Odd ``` count ⇒ currently inside an (possibly unclosed) code fence. */
  function insideFence(text) {
    const ticks = (String(text).match(/```/g) || []).length;
    return ticks % 2 === 1;
  }

  /** Incoming that changes inline HTML and must force a re-parse. */
  function needsInlineReparse(incoming) {
    return /\*\*|`|\]\(|~~/.test(incoming);
  }

  /** Wrap only the newest characters so prior text stays crisp. */
  function markMistTail(root, delta) {
    const n = (delta || "").length;
    if (!n || !root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let last = null;
    while (walker.nextNode()) last = walker.currentNode;
    if (!last || !last.nodeValue) return;
    const text = last.nodeValue;
    const take = Math.min(n, text.length);
    if (take < 1) return;
    const parent = last.parentNode;
    if (!parent) return;
    const head = text.slice(0, text.length - take);
    const tailText = text.slice(text.length - take);
    const mist = makeMistChunk(tailText);
    if (head) {
      const before = document.createTextNode(head);
      parent.replaceChild(before, last);
      parent.insertBefore(mist, before.nextSibling);
    } else {
      parent.replaceChild(mist, last);
    }
  }

  /** While the last block is still a table row, keep the blur+spinner mask on. */
  function syncTableGenerating(block) {
    const el = block?.element;
    if (!el) return;
    const wraps = el.querySelectorAll(".md-table-wrap");
    for (const w of wraps) w.classList.remove("is-generating");
    const text = String(block.text || "").replace(/[ \t]+$/u, "");
    if (!text || !wraps.length) return;
    const lines = text.split("\n");
    let i = lines.length - 1;
    while (i >= 0 && !lines[i].trim()) i -= 1;
    if (i < 0) return;
    const last = lines[i].trim();
    // Still generating when the stream ends on a table row / separator.
    if (last.includes("|")) {
      wraps[wraps.length - 1].classList.add("is-generating");
    }
  }

  function arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    const chunkSize = 0x8000;
    for (let index = 0; index < bytes.length; index += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
    }
    return btoa(binary);
  }

  function formatBytes(value) {
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  }

  function renderAttachments() {
    const list = $("#attachmentList");
    list.innerHTML = "";
    list.hidden = state.attachments.length === 0;
    state.attachments.forEach((attachment, index) => {
      const chip = document.createElement("span");
      chip.className = "attachment-chip";
      const name = document.createElement("span");
      name.className = "name";
      const kind = attachment.is_image || (attachment.mime_type || "").startsWith("image/")
        ? "🖼 " : "📎 ";
      name.textContent = `${kind}${attachment.filename} · ${formatBytes(attachment.size)}`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.title = "移除附件";
      remove.addEventListener("click", () => {
        state.attachments.splice(index, 1);
        renderAttachments();
      });
      chip.append(name, remove);
      list.appendChild(chip);
    });
  }

  async function addAttachments(files) {
    for (const file of files) {
      if (state.attachments.length >= 10) {
        showToast("每次最多添加 10 个附件");
        break;
      }
      if (file.size > 20 * 1024 * 1024) {
        showToast(`${file.name} 超过 20 MiB`);
        continue;
      }
      if (state.attachments.some(item => item.filename === file.name && item.size === file.size)) {
        continue;
      }
      try {
        let dataBase64;
        let mime = file.type || "application/octet-stream";
        let size = file.size;
        let filename = file.name;

        // Images: downscale for multimodal so base64 stays reasonable.
        if (file.type.startsWith("image/") && !/\.svg$/i.test(file.name)) {
          try {
            const resized = await downscaleImageFile(file, 1280, 0.85);
            if (resized) {
              dataBase64 = resized.data_base64;
              mime = resized.mime_type;
              size = resized.size;
              if (resized.filename) filename = resized.filename;
            }
          } catch {
            /* fall through to raw read */
          }
        }
        if (!dataBase64) {
          const buffer = await file.arrayBuffer();
          dataBase64 = arrayBufferToBase64(buffer);
        }
        state.attachments.push({
          filename,
          mime_type: mime,
          size,
          data_base64: dataBase64,
          is_image: mime.startsWith("image/"),
        });
      } catch (error) {
        showToast(`读取附件失败：${file.name}`);
      }
    }
    renderAttachments();
  }

  /** Resize large images client-side before upload (multimodal-friendly). */
  function downscaleImageFile(file, maxSide = 1280, quality = 0.85) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        try {
          const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
          const w = Math.max(1, Math.round(img.width * scale));
          const h = Math.max(1, Math.round(img.height * scale));
          const canvas = document.createElement("canvas");
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext("2d");
          if (!ctx) { resolve(null); return; }
          ctx.drawImage(img, 0, 0, w, h);
          const isPng = file.type === "image/png" && scale === 1;
          const mime = isPng ? "image/png" : "image/jpeg";
          const dataUrl = canvas.toDataURL(mime, quality);
          const comma = dataUrl.indexOf(",");
          const b64 = dataUrl.slice(comma + 1);
          // approximate byte length from base64
          const size = Math.floor(b64.length * 3 / 4);
          resolve({
            data_base64: b64,
            mime_type: mime,
            size,
            filename: file.name.replace(/\.(jpe?g|png|webp)$/i, "") + (mime === "image/png" ? ".png" : ".jpg"),
          });
        } catch (e) {
          reject(e);
        } finally {
          URL.revokeObjectURL(url);
        }
      };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("decode failed")); };
      img.src = url;
    });
  }

  async function loadConversationList() {
    try {
      const response = await fetch("/api/sessions", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      state.conversations = data.sessions || [];
      renderConversationList();
    } catch (error) {
      console.error("conversation list:", error);
    }
  }

  function renderConversationList() {
    const list = $("#conversationList");
    if (!list) return;
    list.innerHTML = "";
    $("#conversationCount").textContent = String(state.conversations.length);
    if (!state.conversations.length) {
      const empty = document.createElement("div");
      empty.className = "conversation-empty muted";
      empty.textContent = "暂无已保存对话";
      list.appendChild(empty);
      return;
    }
    for (const item of state.conversations) {
      const card = document.createElement("div");
      card.className = `conversation-card${item.session_id === state.sessionId ? " active" : ""}`;
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      const copy = document.createElement("div");
      copy.className = "conversation-copy";
      const title = document.createElement("div");
      title.className = "conversation-title";
      title.textContent = item.title || "新对话";
      const meta = document.createElement("div");
      meta.className = "conversation-meta";
      const when = item.updated_at ? new Date(item.updated_at) : null;
      const stamp = when && !Number.isNaN(when.getTime())
        ? when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        : "";
      meta.textContent = `${item.message_count || 0} 条消息${stamp ? ` · ${stamp}` : ""}`;
      copy.append(title, meta);
      const rename = document.createElement("button");
      rename.className = "conversation-rename";
      rename.type = "button";
      rename.title = "编辑标题";
      rename.textContent = "✎";
      rename.addEventListener("click", async event => {
        event.stopPropagation();
        if (state.running) {
          showToast("请先停止当前任务");
          return;
        }
        const nextTitle = window.prompt("编辑会话标题", item.title || "新对话");
        if (nextTitle === null) return;
        const title = nextTitle.trim();
        if (!title) return;
        const response = await fetch("/api/session/title", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: item.session_id, title }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          showToast(data.detail || "标题修改失败");
          return;
        }
        item.title = data.title;
        if (item.session_id === state.sessionId) {
          const topTitle = $("#topTitle");
          if (topTitle) topTitle.textContent = data.title;
        }
        renderConversationList();
        showToast("会话标题已更新");
      });
      const remove = document.createElement("button");
      remove.className = "conversation-delete";
      remove.type = "button";
      remove.title = "删除对话";
      remove.textContent = "×";
      remove.addEventListener("click", async event => {
        event.stopPropagation();
        if (state.running) {
          showToast("请先停止当前任务");
          return;
        }
        const response = await fetch(`/api/session?session_id=${encodeURIComponent(item.session_id)}`, { method: "DELETE" });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          showToast(data.detail || "删除失败");
          return;
        }
        if (item.session_id === state.sessionId) {
          state.sessionId = randomId();
          localStorage.setItem("spark_session_id", state.sessionId);
          state.messages = [];
          state.plan = [];
          state.live = null;
          renderSavedMessages();
          renderPlan();
          await loadSession();
        }
        await loadConversationList();
      });
      const activate = async () => {
        if (state.running) {
          showToast("请先停止当前任务");
          return;
        }
        if (item.session_id === state.sessionId) return;
        state.sessionId = item.session_id;
        localStorage.setItem("spark_session_id", state.sessionId);
        state.messages = [];
        state.plan = [];
        state.live = null;
        state.attachments = [];
        renderAttachments();
        setSaveStatus("正在恢复对话…", "pending");
        await loadSession();
        await loadConversationList();
        sidebar.classList.remove("open");
      };
      card.addEventListener("click", activate);
      card.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activate();
        }
      });
      card.append(copy, rename, remove);
      list.appendChild(card);
    }
  }

  async function loadSettings() {
    if (!document.getElementById("settingContext")) return;
    try {
      const response = await fetch("/api/settings", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      state.settings = data.settings || {};
      const map = {
        settingContext: "context_size",
        settingProfile: "backend_profile",
        settingGpuLayers: "gpu_layers",
        settingThreads: "threads",
        settingBatch: "threads_batch",
        settingFlash: "flash_attention",
        settingThreshold: "compaction_threshold",
        settingMaxTokens: "max_tokens",
      };
      for (const [id, key] of Object.entries(map)) {
        const node = document.getElementById(id);
        if (node && state.settings[key] !== undefined) node.value = String(state.settings[key]);
      }
      const auto = document.getElementById("settingAutoCompact");
      if (auto) auto.checked = Boolean(state.settings.auto_compaction);
      const visionSel = document.getElementById("settingVision");
      if (visionSel) visionSel.value = state.settings.enable_vision === false ? "0" : "1";
      const mtpSel = document.getElementById("settingMtp");
      if (mtpSel) mtpSel.value = state.settings.enable_mtp === false ? "0" : "1";
      const specSel = document.getElementById("settingSpec");
      if (specSel) specSel.value = state.settings.enable_speculative ? "1" : "0";
      fillModelSelect(data.models || [], state.settings.model);
      state._modelsCache = data.models || [];
      // Portable runtime fields
      const binInput = document.getElementById("settingLlamaBin");
      if (binInput) binInput.value = String(data.llama_bin ?? state.settings.llama_bin ?? "");
      const dirInput = document.getElementById("settingModelDir");
      if (dirInput) dirInput.value = String(data.model_dir ?? state.settings.model_dir ?? "");
      const binHint = document.getElementById("llamaBinHint");
      if (binHint) {
        const resolved = data.llama_bin_resolved || "";
        binHint.textContent = resolved
          ? `当前生效：${resolved}`
          : "未探测到 llama-server，请填写完整路径并保存。";
      }
      // Apply capability visibility AFTER values are set (so visible selects keep saved prefs).
      const caps = data.capabilities || data.model_info || null;
      renderModelInfo(data.model_info, data.model_dir);
      if (caps) renderCapBadges({ ...(data.model_info || {}), ...caps });
      else renderCapBadges(null);
      if (window.SP) {
        const modal = document.getElementById("appModal");
        if (modal) SP.enhanceAll(modal);
        modal?.querySelectorAll("select[data-sp-done]").forEach((s) => SP.refresh(s));
      }
    } catch (error) {
      showToast("无法加载设置：" + error.message);
    }
  }

  function formatBytes(n) {
    if (n == null || n === "") return "—";
    let v = Number(n);
    if (Number.isNaN(v)) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i += 1;
    }
    return i === 0 ? `${v} ${units[i]}` : `${v.toFixed(2)} ${units[i]}`;
  }

  function renderModelInfo(info, dir) {
    const box = document.getElementById("modelInfo");
    if (!box) return;
    if (!info || info.error) {
      box.innerHTML = `<div class="sp-model-name">未选择有效模型</div>
        <div class="sp-model-meta">${escapeHtml(info?.error || "请在下方选择 .gguf 文件")}</div>`;
      renderCapBadges(null);
      return;
    }
    const labels = info.labels?.length
      ? info.labels
      : [
          info.text !== false && !info.diffusion ? "文本" : null,
          info.vision ? "视觉" : null,
          info.mtp ? "MTP" : null,
          info.diffusion ? "扩散" : null,
          info.embedding ? "Embedding" : null,
          info.reranker ? "Reranker" : null,
        ].filter(Boolean);
    const bits = [];
    if (info.quant) bits.push(info.quant);
    bits.push(formatBytes(info.size_bytes));
    if (info.architecture) bits.push(info.architecture);
    box.innerHTML = `<div class="sp-model-name">${escapeHtml(info.name || "")}</div>
      <div class="sp-model-meta">${escapeHtml(bits.join(" · "))}</div>
      <div class="sp-model-path">${escapeHtml(info.path || "")}</div>` +
      (info.mmproj
        ? `<div class="sp-model-meta">投影器：${escapeHtml(String(info.mmproj).split("/").pop())}` +
          `${info.mmproj_size_human ? " · " + escapeHtml(info.mmproj_size_human) : ""}</div>`
        : "");
    renderCapBadges(info);
    const hint = document.getElementById("modelDirHint");
    if (hint && dir) hint.textContent = `扫描目录：${dir}（视觉模型会自动配对 mmproj）`;
  }

  function renderCapBadges(info) {
    const box = document.getElementById("capBadges");
    const hint = document.getElementById("capHint");
    const empty = document.getElementById("capEmpty");
    const settingsGrid = document.getElementById("capSettings");
    if (!box) return;

    if (!info || info.error) {
      box.innerHTML = "";
      if (hint) hint.hidden = true;
      if (empty) {
        empty.hidden = false;
        empty.textContent = "请选择有效模型以识别能力。";
      }
      if (settingsGrid) settingsGrid.hidden = true;
      setCapControlState({ vision: false, mtp: false, diffusion: false });
      return;
    }

    const tags = [];
    const push = (on, text, cls) => {
      if (on) tags.push(`<span class="sp-tag ${cls || ""}">${escapeHtml(text)}</span>`);
    };
    push(info.vision, "视觉", "sp-tag-vision");
    push(info.mtp, "MTP", "sp-tag-mtp");
    push(info.diffusion, "扩散", "sp-tag-diff");
    push(info.embedding, "Embedding", "sp-tag-emb");
    push(info.reranker, "Reranker", "sp-tag-emb");
    // Only show 文本 tag when it's meaningful (chat text model, not pure special types alone)
    const isPlainChat = (info.text !== false || info.vision) && !info.diffusion;
    if (isPlainChat && !info.embedding && !info.reranker) push(true, "文本", "");
    if (!tags.length) tags.push(`<span class="sp-tag">未知</span>`);
    box.innerHTML = tags.join("");

    setCapControlState({
      vision: !!info.vision,
      mtp: !!info.mtp,
      diffusion: !!info.diffusion,
      embedding: !!info.embedding,
      spec: !!info.mtp,
    });

    const hasOptions = !!(info.vision || info.mtp);
    if (settingsGrid) settingsGrid.hidden = !hasOptions;
    if (empty) empty.hidden = hasOptions;
    if (!hasOptions) {
      if (empty) {
        empty.textContent = info.diffusion
          ? "扩散模型：无 mmproj / MTP 对话开关。"
          : info.embedding || info.reranker
            ? "Embedding / Reranker 模型：无聊天能力开关。"
            : "当前为纯文本模型：无视觉 mmproj、MTP 等可调项。";
      }
      if (hint) hint.hidden = true;
      return;
    }

    const notes = [];
    if (info.vision) notes.push("仅显示：视觉 mmproj 开关");
    if (info.mtp) notes.push(`仅显示：MTP（${info.mtp_tensors?.length || 0} 个 nextn 张量）与投机解码`);
    if (info.diffusion) notes.push("含扩散能力，不适合纯文本聊天");
    if (hint) {
      hint.hidden = false;
      hint.textContent = notes.join("；") || "已按模型能力显示开关。";
    }
  }

  function fieldHidden(id) {
    const field = document.getElementById(id);
    if (!field) return true;
    // native hidden OR CSS-hidden parent field
    if (field.hidden) return true;
    const select = field.querySelector("select");
    if (select && select.disabled) return true;
    return false;
  }

  function setCapControlState(info) {
    const pairs = [
      { field: "fieldVision", select: "settingVision", on: !!info.vision },
      { field: "fieldMtp", select: "settingMtp", on: !!info.mtp },
      { field: "fieldSpec", select: "settingSpec", on: !!info.spec || !!info.mtp },
    ];
    for (const { field: fieldId, select: selectId, on } of pairs) {
      const field = document.getElementById(fieldId);
      const select = document.getElementById(selectId);
      if (field) field.hidden = !on;
      if (select) {
        select.disabled = !on;
        if (!on) {
          // reset to safe default so a hidden stale value isn't saved
          if (selectId === "settingVision") select.value = "1";
          else select.value = "0";
          // refresh custom dropdown label if present
          if (window.SP) {
            try { SP.refresh(select); } catch { /* ignore */ }
          }
        } else if (window.SP) {
          try { SP.refresh(select); } catch { /* ignore */ }
        }
      }
    }
  }

  function fillModelSelect(models, current) {
    const sel = document.getElementById("settingModel");
    if (!sel) return;
    sel.innerHTML = "";
    if (!models.length) {
      sel.innerHTML = `<option value="">（目录中没有 .gguf）</option>`;
      sel.disabled = true;
      if (window.SP) SP.refresh(sel);
      return;
    }
    sel.disabled = false;
    for (const m of models) {
      const opt = document.createElement("option");
      opt.value = m.path;
      const flags = [];
      if (m.vision) flags.push("视觉");
      if (m.mtp) flags.push("MTP");
      if (m.diffusion) flags.push("扩散");
      if (m.embedding) flags.push("Embed");
      if (m.quant) flags.push(m.quant);
      opt.textContent = `${m.name}  ·  ${m.size_human}${flags.length ? "  ·  " + flags.join(" · ") : ""}`;
      opt.dataset.size = m.size_human || "";
      opt.dataset.quant = m.quant || "";
      opt.dataset.vision = m.vision ? "1" : "";
      opt.dataset.mtp = m.mtp ? "1" : "";
      opt.dataset.diffusion = m.diffusion ? "1" : "";
      opt.dataset.mmproj = m.mmproj || "";
      if (m.path === current || m.selected) opt.selected = true;
      sel.appendChild(opt);
    }
    if (window.SP) SP.refresh(sel);
  }

  async function saveSettings() {
    const btn = document.getElementById("saveSettings");
    if (!btn) return;
    if (state.running) {
      showToast("请先停止当前任务");
      return;
    }
    if (state._savingSettings) return;
    const values = {
      context_size: Number($("#settingContext").value),
      backend_profile: $("#settingProfile").value,
      gpu_layers: $("#settingGpuLayers").value,
      threads: Number($("#settingThreads").value),
      threads_batch: Number($("#settingBatch").value),
      flash_attention: $("#settingFlash").value,
      auto_compaction: $("#settingAutoCompact").checked,
      compaction_threshold: Number($("#settingThreshold").value),
      max_tokens: Number($("#settingMaxTokens").value),
    };
    // Only persist capability toggles when the matching option is visible for this model.
    const visionSel = document.getElementById("settingVision");
    if (visionSel && !fieldHidden("fieldVision") && !visionSel.disabled) {
      values.enable_vision = visionSel.value === "1";
    }
    const mtpSel = document.getElementById("settingMtp");
    if (mtpSel && !fieldHidden("fieldMtp") && !mtpSel.disabled) {
      values.enable_mtp = mtpSel.value === "1";
    }
    const specSel = document.getElementById("settingSpec");
    if (specSel && !fieldHidden("fieldSpec") && !specSel.disabled) {
      values.enable_speculative = specSel.value === "1";
    }
    const binInput = document.getElementById("settingLlamaBin");
    if (binInput) values.llama_bin = String(binInput.value || "").trim();
    const dirInput = document.getElementById("settingModelDir");
    if (dirInput) {
      const dir = String(dirInput.value || "").trim();
      if (dir) values.model_dir = dir;
    }
    const modelSel = document.getElementById("settingModel");
    if (modelSel && modelSel.value) values.model = modelSel.value;
    // Reject NaN before hitting the API (stepper can be cleared).
    for (const [k, v] of Object.entries(values)) {
      if (typeof v === "number" && Number.isNaN(v)) {
        showToast(`请填写有效的 ${k}`);
        return;
      }
    }
    state._savingSettings = true;
    btn.disabled = true;
    const prevLabel = btn.textContent;
    btn.textContent = "保存并重启中…";
    const status = document.getElementById("settingsStatus");
    if (status) status.textContent = "写入设置并重启 llama.cpp（加载模型可能需数十秒）…";
    showToast("正在保存并重启 llama.cpp…");
    try {
      const response = await fetch("/api/settings/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      state.settings = data.settings || values;
      if (status) status.textContent = "已保存并重启 llama.cpp，参数已生效";
      showToast("设置已保存，llama.cpp 已按新参数重启");
      await health();
      await loadSettings();
    } catch (error) {
      if (status) status.textContent = "失败：" + (error.message || "未知错误");
      showToast("设置保存失败：" + (error.message || "未知错误"));
    } finally {
      state._savingSettings = false;
      btn.disabled = false;
      btn.textContent = prevLabel;
    }
  }

  function renderHealth(data) {
    const health = data.model_health || {};
    const online = Boolean(health.model_healthy && health.llama_healthy);
    const manualStopped = Boolean(data.llama_manual_stopped || health.llama_manual_stopped);
    $("#statusDot").className = `status-dot ${online ? "online" : "offline"}`;
    const mf = data.model_file || health.model_file || {};
    const modelName = mf.name || data.model || "Spark";
    // Dot color already conveys health; keep the line to the model name only.
    $("#statusText").textContent = modelName;
    $("#statusText").title = `${mf.path || modelName}${online ? "" : manualStopped ? " · 已手动停止" : " · 异常"}`;
    const badge = $("#modelHealthBadge");
    if (badge) {
      badge.textContent = online ? "HEALTHY" : manualStopped ? "STOPPED" : "OFFLINE";
      badge.className = `health-badge ${online ? "ok" : "bad"}`;
    }
    updateLlamaToggle(online, manualStopped);
    const used = Number(health.context_used || 0);
    const total = Number(health.context_total || state.settings.context_size || 0);
    const percent = Number(health.context_percent || (total ? (100 * used / total) : 0));
    const usage = $("#contextUsage");
    const bar = $("#contextBar");
    if (usage) usage.textContent = `${used.toLocaleString()} / ${total.toLocaleString()}`;
    const ring = $("#contextRing");
    const ringPercent = $("#contextPercent");
    if (ring) ring.style.setProperty("--progress", `${Math.min(360, Math.max(0, percent * 3.6))}deg`);
    if (ringPercent) ringPercent.textContent = `${percent.toFixed(percent < 10 ? 1 : 0)}%`;
    if (bar) {
      bar.style.width = `${Math.min(100, Math.max(0, percent))}%`;
      bar.className = `context-bar ${percent >= 80 ? "danger" : percent >= 60 ? "warn" : ""}`;
    }
    const meta = $("#runtimeMeta");
    if (meta) {
      meta.textContent = `${health.backend_profile || "auto"} · KV ${health.kv_cache || "unknown"} · GPU ${health.settings?.gpu_layers || "auto"}`;
    }
    // Don't toast every poll when the user intentionally stopped the model.
    if (!online && !manualStopped && !state._offlineToastAt) {
      state._offlineToastAt = Date.now();
      showToast("模型或 llama.cpp 不健康，请检查启动日志。");
      setTimeout(() => { state._offlineToastAt = 0; }, 30000);
    } else if (online) {
      state._offlineToastAt = 0;
    }
  }

  function updateLlamaToggle(online, manualStopped) {
    const btn = document.getElementById("stopLlama");
    if (!btn || btn.disabled) {
      // Still update labels when not mid-action
    }
    if (!btn) return;
    if (btn.dataset.busy === "1") return;
    const shouldStart = !online || manualStopped;
    btn.classList.toggle("start-llama", shouldStart);
    btn.classList.toggle("stop-llama", !shouldStart);
    btn.textContent = shouldStart ? "启动模型" : "停止模型";
    btn.title = shouldStart
      ? "启动主模型 llama.cpp（读取当前设置）"
      : "停止主模型 llama.cpp（手动停止后不会自动重启）";
    btn.dataset.mode = shouldStart ? "start" : "stop";
  }

  function setSaveStatus(text, mode = "") {
    const node = $("#saveStatus");
    if (!node) return;
    node.textContent = text;
    node.className = `save-status ${mode}`.trim();
  }

  function setLoop(text, step = state.loopStep, mode = "running") {
    state.loopStep = Math.max(state.loopStep, Number(step) || 0);
    $("#loopCount").textContent = String(state.loopStep);
    const body = $("#loopBody");
    body.textContent = text;
    body.className = `loop-body ${mode}`;
  }

  function setWorkspaceDisplay(path) {
    if (!path) return;
    const input = $("#workspaceInput");
    if (!input) return;
    if (document.activeElement !== input) input.value = path;
    input.title = path;
  }

  async function switchWorkspace() {
    if (state.running) {
      showToast("请等待当前请求完成后再切换工作目录");
      return;
    }
    const path = $("#workspaceInput").value.trim();
    if (!path) {
      showToast("请输入要切换到的绝对目录");
      $("#workspaceInput").focus();
      return;
    }
    const button = $("#switchWorkspace");
    button.disabled = true;
    button.textContent = "切换中…";
    try {
      const response = await fetch("/api/workspace", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      setWorkspaceDisplay(data.workspace);
      $("#workspaceHint").textContent = "已切换；后续工具和模型提示将使用该目录。";
      showToast(`工作目录已切换到 ${data.workspace}`);
      await health();
    } catch (error) {
      showToast("切换失败：" + (error.message || "未知错误"));
    } finally {
      button.disabled = false;
      button.textContent = "切换";
    }
  }

  function renderPlan() {
    const body = $("#planBody");
    const count = $("#planCount");
    const plan = state.plan?.[0];
    if (!plan || !plan.steps?.length) {
      body.className = "plan-body muted";
      body.textContent = "复杂任务会在这里显示步骤。";
      count.textContent = "0";
      return;
    }
    body.className = "plan-body";
    const done = plan.steps.filter(step => step.done).length;
    count.textContent = `${done}/${plan.steps.length}`;
    body.innerHTML = `<div class="plan-title">${escapeHtml(plan.title)}</div>` +
      plan.steps.map(step => `
        <div class="plan-step ${step.done ? "done" : ""}">
          <span class="plan-check">${step.done ? "✓" : ""}</span>
          <span>${escapeHtml(step.text)}</span>
        </div>`).join("");
  }

  function parseSseBuffer(buffer) {
    const frames = buffer.split("\n\n");
    const pending = frames.pop() || "";
    for (const frame of frames) {
      const data = frame.split("\n")
        .filter(line => line.startsWith("data:"))
        .map(line => line.slice(5).trim())
        .join("\n");
      if (data) {
        let parsed;
        try {
          parsed = JSON.parse(data);
        } catch (error) {
          console.error(error);
          continue;
        }
        handleEvent(parsed);
      }
    }
    return pending;
  }

  async function consumeSse(response) {
    if (!response.ok || !response.body) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = parseSseBuffer(buffer);
    }
    if (buffer.trim()) parseSseBuffer(buffer + "\n\n");
  }

  async function stopActiveRun() {
    try {
      const response = await fetch("/api/run/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.sessionId }),
      });
      return response.ok;
    } catch {
      return false;
    }
  }

  function beginLiveTurn({ userDisplay = null } = {}) {
    state.running = true;
    state.live = null;
    state.loopStep = 0;
    setSaveStatus("保存中…", "pending");
    setLoop("等待模型…", 0, "running");
    updateSendButton();
    if (userDisplay != null) {
      createMessage("user", userDisplay);
      state.messages.push({ role: "user", content: userDisplay });
    }
    createMessage("assistant", "");
    state.controller = new AbortController();
    startLiveContextPolling();
  }

  async function finishLiveTurn({ aborted = false } = {}) {
    stopLiveContextPolling();
    // Finish markdown parse on live blocks before tearing them down.
    finalizeLiveMarkdown(state.live);
    clearMist();
    state.live = null;
    state.attachments = [];
    renderAttachments();
    localStorage.setItem("spark_session_id", state.sessionId);
    try {
      // Success: reload authoritative state from the server (worker already saved).
      // Abort: keep local messages so a partial turn is not wiped.
      state.plan = await loadSession(!aborted);
      renderPlan();
      setSaveStatus(aborted ? "已停止" : "已保存");
      await loadConversationList();
      // Refresh sidebar context ring from llama slots.
      health().catch(() => {});
    } catch {
      setSaveStatus(aborted ? "已停止" : "保存失败", aborted ? "" : "bad");
    }
    state.running = false;
    state.controller = null;
    updateSendButton();
  }

  async function send(messageText) {
    const entered = String(messageText || "").trim();
    const text = entered || (state.attachments.length ? "请分析这些附件。" : "");
    if (!text || state.running) return;

    const attachments = state.attachments.map(item => ({
      filename: item.filename,
      mime_type: item.mime_type,
      data_base64: item.data_base64,
    }));
    const userDisplay = attachments.length
      ? `${text}\n\n📎 ${attachments.map(item => item.filename).join("、")}`
      : text;

    beginLiveTurn({ userDisplay });

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          session_id: state.sessionId,
          attachments,
        }),
        signal: state.controller.signal,
      });
      await consumeSse(response);
      await finishLiveTurn();
    } catch (error) {
      const aborted = error.name === "AbortError";
      if (!aborted) {
        setSaveStatus("保存失败", "bad");
        showToast(error.message || "请求失败");
        const root = messagesEl.querySelector(".message.assistant:last-child .message-content");
        if (root) {
          root.innerHTML = `<p style="color:var(--red)">⚠ ${escapeHtml(error.message || "请求失败")}</p>`;
        }
      } else {
        await stopActiveRun();
        const root = messagesEl.querySelector(".message.assistant:last-child .message-content");
        if (root && !root.textContent.trim()) {
          root.innerHTML = "<p class='muted'>已停止。</p>";
        }
      }
      state.live = null;
      state.running = false;
      state.controller = null;
      stopLiveContextPolling();
      updateSendButton();
      try {
        state.plan = await loadSession(true);
        renderPlan();
        await loadConversationList();
        health().catch(() => {});
      } catch {
        /* ignore refresh failures after abort */
      }
      if (!aborted) promptEl.focus();
    }
  }

  async function resumeRunningSession() {
    try {
      const response = await fetch(`/api/run?session_id=${encodeURIComponent(state.sessionId)}`, {
        cache: "no-store",
      });
      const data = await response.json().catch(() => ({}));
      if (!data.running) {
        // A run may have finished between page load and this check.
        state.plan = await loadSession();
        renderPlan();
        await loadConversationList();
        return false;
      }
    } catch {
      return false;
    }

    beginLiveTurn();
    setSaveStatus("已恢复任务流", "pending");
    setLoop("正在恢复连接…", 0, "running");
    showToast("检测到后台任务，已重新连接");

    try {
      const response = await fetch(
        `/api/chat/stream?session_id=${encodeURIComponent(state.sessionId)}`,
        { signal: state.controller.signal },
      );
      await consumeSse(response);
      await finishLiveTurn();
      return true;
    } catch (error) {
      state.live = null;
      state.running = false;
      state.controller = null;
      stopLiveContextPolling();
      updateSendButton();
      if (error.name !== "AbortError") {
        try {
          state.plan = await loadSession();
          renderPlan();
          await loadConversationList();
          health().catch(() => {});
        } catch {
          /* ignore */
        }
      }
      return false;
    }
  }

  function ensureLiveTimeline(live) {
    if (live.timelineReady) return;
    live.view.body.innerHTML = "";
    if (live.view.reasoning) live.view.reasoning.style.display = "none";
    if (live.view.tools) live.view.tools.style.display = "none";
    live.timelineReady = true;
  }

  /** Structural Markdown that requires a full re-parse (not every token). */
  const STRUCT_MD =
    /(\n\s*(?:[-*+]|\d+\.)\s|\n#{1,6}\s|```|\n\||\n>\s|\n{2,})|^\s*\|/;

  function createLiveTextBlock(live, kind) {
    const element = document.createElement("div");
    element.className = "message-content live-text-block";
    live.view.body.appendChild(element);
    const block = { element, text: "" };
    if (kind === "reasoning") live.currentReasoningBlock = block;
    else live.currentTextBlock = block;
    return block;
  }

  function createLiveReasoningBlock(live, round) {
    const wrap = makeReasoningNode(`第 ${round} 轮思考`, "", true);
    live.view.body.appendChild(wrap);
    const block = {
      element: wrap.querySelector(".reasoning-body"),
      text: "",
      details: wrap,
    };
    live.currentReasoningBlock = block;
    live.reasoningBlocks.push(block);
    return block;
  }

  /**
   * Stream with working mist:
   * - Non-structural tokens → append a mist-chunk (animation runs on a stable DOM).
   * - Structural Markdown (``` ** list table…) → re-parse whole block, mist only the new tail.
   */
  function appendLiveText(block, delta, markdown = true) {
    const incoming = delta || "";
    if (!incoming) return;
    block.text += incoming;

    if (!markdown) {
      getStreamTail(block.element).appendChild(makeMistChunk(incoming));
      scrollBottom();
      return;
    }

    // Re-parse when:
    // - structure changes (lists, fences, tables…)
    // - inside a code fence (content must stay in <pre>)
    // - inline markers arrive (** ` [text](url)) so bold/code actually render
    const structural =
      STRUCT_MD.test(incoming) ||
      insideFence(block.text) ||
      needsInlineReparse(incoming) ||
      !block.element.firstChild;

    if (structural) {
      if (block._mdTimer) {
        clearTimeout(block._mdTimer);
        block._mdTimer = null;
      }
      block.element.innerHTML = renderMarkdown(block.text);
      markMistTail(block.element, incoming);
    } else {
      getStreamTail(block.element).appendChild(makeMistChunk(incoming));
    }
    syncTableGenerating(block);
    scrollBottom();
  }

  /** Final re-parse of every live markdown block (call on done / finish). */
  function finalizeLiveMarkdown(live) {
    if (!live) return;
    const blocks = [];
    if (live.currentTextBlock) blocks.push(live.currentTextBlock);
    if (live.currentReasoningBlock) blocks.push(live.currentReasoningBlock);
    if (live.reasoningBlock) blocks.push(live.reasoningBlock);
    for (const b of live.reasoningBlocks || []) blocks.push(b);
    for (const b of blocks) {
      if (b && b.element && b.text) {
        b.element.innerHTML = renderMarkdown(b.text);
      }
    }
    if (live.reasoning && live.reasoningEl) {
      const body = live.reasoningEl.querySelector(".reasoning-body");
      if (body) body.innerHTML = renderMarkdown(live.reasoning);
    }
  }

  function clearMist() {
    for (const el of messagesEl.querySelectorAll(".mist-chunk")) {
      const parent = el.parentNode;
      if (!parent) continue;
      parent.replaceChild(document.createTextNode(el.textContent || ""), el);
      parent.normalize();
    }
    for (const el of messagesEl.querySelectorAll(".stream-tail")) {
      if (!el.childNodes.length) el.remove();
    }
    for (const el of messagesEl.querySelectorAll(".md-table-wrap.is-generating")) {
      el.classList.remove("is-generating");
    }
    for (const el of messagesEl.querySelectorAll(".mist-veil, .token-born")) {
      el.remove();
    }
    for (const el of messagesEl.querySelectorAll(".mist-layer")) {
      el.innerHTML = "";
    }
    for (const el of messagesEl.querySelectorAll(".is-streaming")) {
      el.classList.remove("is-streaming");
    }
  }

  function applyPlanArgs(args) {
    if (!args || typeof args !== "object") return;
    const completed = new Set(
      (args.completed_steps || []).map(value => Number(value))
    );
    state.plan = [{
      title: args.title || "执行计划",
      steps: (args.steps || []).map((text, index) => ({
        index: index + 1,
        text: String(text),
        done: completed.has(index + 1),
      })),
    }];
    renderPlan();
  }

  function renderAskCard(live, event) {
    const key = String(event.key || "");
    if (!key || live.view.body.querySelector(`[data-ask-key="${CSS.escape(key)}"]`)) return;
    const card = document.createElement("div");
    card.className = "ask-card";
    card.dataset.askKey = key;
    const heading = document.createElement("div");
    heading.className = "ask-heading";
    heading.innerHTML = "<span class=\'ask-icon\'>💬</span><span>需要你的确认</span>";
    const question = document.createElement("div");
    question.className = "ask-question";
    question.textContent = event.question || "请回答这个问题";
    card.append(heading, question);

    const answer = document.createElement("input");
    answer.className = "ask-input";
    answer.type = "text";
    answer.placeholder = event.options?.length ? "选择选项，或输入自定义答案…" : "输入你的回答…";
    answer.autocomplete = "off";
    card.appendChild(answer);

    let optionsEl = null;
    if (Array.isArray(event.options) && event.options.length) {
      const options = document.createElement("div");
      optionsEl = options;
      options.className = "ask-options";
      for (const option of event.options) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "ask-option";
        button.textContent = option;
        button.addEventListener("click", () => {
          answer.value = option;
          options.querySelectorAll(".ask-option").forEach(item => item.classList.remove("selected"));
          button.classList.add("selected");
          answer.focus();
        });
        options.appendChild(button);
      }
      card.appendChild(options);
    }

    const footer = document.createElement("div");
    footer.className = "ask-footer";
    const status = document.createElement("span");
    status.className = "ask-status";
    status.textContent = "等待回答";
    const submit = document.createElement("button");
    submit.type = "button";
    submit.className = "ask-submit";
    submit.textContent = "提交回答";
    submit.addEventListener("click", async () => {
      const value = answer.value.trim();
      if (!value) {
        status.textContent = "请先输入或选择答案";
        answer.focus();
        return;
      }
      submit.disabled = true;
      answer.disabled = true;
      optionsEl?.querySelectorAll("button").forEach(button => { button.disabled = true; });
      status.textContent = "正在提交…";
      try {
        const response = await fetch("/api/answer", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key, answer: value }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        card.classList.add("answered");
        status.textContent = "已回答";
        showToast("已提交你的回答");
      } catch (error) {
        submit.disabled = false;
        answer.disabled = false;
        optionsEl?.querySelectorAll("button").forEach(button => { button.disabled = false; });
        status.textContent = error.message || "提交失败";
      }
    });
    footer.append(status, submit);
    card.appendChild(footer);
    live.view.body.appendChild(card);
    scrollBottom();
    answer.focus();
  }

  function applyContextStatus(used, total, autoCompacted) {
    const totalN = Number(total || state.settings.context_size || 0);
    const usedN = Number(used || 0);
    const percent = Number(
      (totalN ? (100 * usedN) / totalN : 0).toFixed(1),
    );
    const usage = $("#contextUsage");
    if (usage) usage.textContent = `${usedN.toLocaleString()} / ${totalN.toLocaleString()}`;
    const ring = $("#contextRing");
    const ringPercent = $("#contextPercent");
    if (ring) ring.style.setProperty("--progress", `${Math.min(360, Math.max(0, percent * 3.6))}deg`);
    if (ringPercent) ringPercent.textContent = `${percent.toFixed(percent < 10 ? 1 : 0)}%`;
    const bar = $("#contextBar");
    if (bar) {
      bar.style.width = `${Math.min(100, Math.max(0, percent))}%`;
      bar.className = `context-bar ${percent >= 80 ? "danger" : percent >= 60 ? "warn" : ""}`;
    }
    if (autoCompacted) showToast("上下文已自动压缩");
    state.contextUsed = usedN;
    state.contextTotal = totalN;
  }

  /** Real token counts from llama /slots (prompt + decoded so far) only — no char estimates. */
  async function pollLiveContextOnce() {
    try {
      const response = await fetch("/api/context/live", { cache: "no-store" });
      if (!response.ok) return false;
      const data = await response.json();
      if (data.used == null || data.used === 0 && data.prompt_tokens === 0) {
        // Keep previous display until slots report something useful.
        if (!data.ok) return false;
      }
      applyContextStatus(data.used, data.total, false);
      return true;
    } catch {
      return false;
    }
  }

  function startLiveContextPolling() {
    stopLiveContextPolling();
    pollLiveContextOnce();
    state._ctxTimer = setInterval(() => {
      if (!state.running) {
        stopLiveContextPolling();
        return;
      }
      pollLiveContextOnce();
    }, 600);
  }

  function stopLiveContextPolling() {
    if (state._ctxTimer) {
      clearInterval(state._ctxTimer);
      state._ctxTimer = null;
    }
  }

  function handleEvent(event) {
    // Context numbers come only from /api/context/live (llama slots).
    // context_status is used solely for the auto-compaction toast.
    if (event.type === "context_status") {
      if (event.auto_compacted) showToast("上下文已自动压缩");
      return;
    }

    const live = currentLiveMessage();
    if (!live) return;
    ensureLiveTimeline(live);

    if (event.type === "plan_preview") {
      applyPlanArgs(event.args);
    } else if (event.type === "ask_user") {
      renderAskCard(live, event);
    } else if (event.type === "loop_start") {
      live.round = Number(event.step) || live.round + 1;
      live.currentReasoningBlock = null;
      live.currentTextBlock = null;
      setLoop(`第 ${live.round} 轮 · 模型推理中`, live.round, "running");
      // Prefill often done by now — pull real prompt token count immediately.
      pollLiveContextOnce();
    } else if (event.type === "loop_end") {
      setReasoningOpen(live.reasoningEl, false);
      const current = live.currentReasoningBlock?.details;
      setReasoningOpen(current, false);
      setLoop(`第 ${event.step} 轮 · 已完成`, event.step, "done");
      pollLiveContextOnce();
    } else if (event.type === "reasoning_delta") {
      const round = Number(event.step) || live.round;
      if (round <= 1) {
        if (!live.reasoningEl) {
          const wrap = makeReasoningNode("思考", "", true);
          live.view.body.appendChild(wrap);
          live.reasoningEl = wrap;
          live.reasoningBlock = {
            element: wrap.querySelector(".reasoning-body"),
            text: "",
            details: wrap,
          };
        }
        live.reasoning += event.delta || "";
        const rb = live.reasoningBlock;
        rb.text = live.reasoning;
        const incoming = event.delta || "";
        const structural =
          !rb.element.firstChild ||
          STRUCT_MD.test(incoming) ||
          insideFence(rb.text) ||
          needsInlineReparse(incoming);
        if (structural) {
          rb.element.innerHTML = renderMarkdown(rb.text);
          markMistTail(rb.element, incoming);
        } else {
          getStreamTail(rb.element).appendChild(makeMistChunk(incoming));
        }
        scrollBottom();
      } else {
        const block = live.currentReasoningBlock || createLiveReasoningBlock(live, round);
        appendLiveText(block, event.delta, true);
      }
      scrollBottom();
    } else if (event.type === "delta") {
      const block = live.currentTextBlock || createLiveTextBlock(live, "text");
      live.content += event.delta || "";
      appendLiveText(block, event.delta, true);
      // Context ring updates only via /api/context/live polling.
    } else if (event.type === "tool_call") {
      const step = Number(event.step) || live.round || 1;
      setLoop(`第 ${step} 轮 · 调用 ${event.name}`, step, "running");
      const tool = { id: event.id, name: event.name, args: event.args, result: null, step };
      // Close the current text/reasoning segment so later tokens start a NEW
      // block AFTER this tool card — same order as persisted `blocks[]`.
      live.currentTextBlock = null;
      live.currentReasoningBlock = null;
      renderTool(live.view.body, tool);
      live.tools.push(tool);
    } else if (event.type === "tool_result") {
      const tool = live.tools.find(item => item.id === event.id);
      if (tool) {
        tool.result = event.content;
        tool.outcome = event.outcome;
      }
      const step = Number(event.step) || tool?.step || live.round || 1;
      const failed = event.outcome && event.outcome !== "success";
      setLoop(`第 ${step} 轮 · ${event.name} ${failed ? "失败" : "已返回"}`, step, "running");
      renderTool(live.view.body, {
        ...(tool || event),
        id: event.id,
        name: event.name,
        args: tool?.args,
        result: event.content,
        outcome: event.outcome,
      });
      pollLiveContextOnce();
    } else if (event.type === "done") {
      if (event.message?.reasoning && !live.reasoning) {
        live.reasoning = event.message.reasoning;
      }
      if (event.message?.content && !live.currentTextBlock && !live.content.includes(event.message.content)) {
        const block = createLiveTextBlock(live, "text");
        live.content += event.message.content;
        appendLiveText(block, event.message.content, true);
      }
      finalizeLiveMarkdown(live);
      clearMist();
      setReasoningOpen(live.reasoningEl, false);
      for (const block of live.reasoningBlocks || []) setReasoningOpen(block.details, false);
      state.sessionId = event.session_id || state.sessionId;
      if (event.title) {
        const topTitle = $("#topTitle");
        if (topTitle) topTitle.textContent = event.title;
        const active = state.conversations.find(item => item.session_id === state.sessionId);
        if (active) active.title = event.title;
        renderConversationList();
      }
      if (state.loopStep) setLoop(`完成 · 共 ${state.loopStep} 轮`, state.loopStep, "done");
      localStorage.setItem("spark_session_id", state.sessionId);
      // Pull real slot usage from llama after the turn.
      health().catch(() => {});
    } else if (event.type === "error") {
      throw new Error(event.message || "Agent 运行失败");
    }
  }

  function currentLiveMessage() {
    if (!state.live) {
      const nodes = messagesEl.querySelectorAll(".message.assistant");
      const root = nodes[nodes.length - 1];
      if (!root) return null;
      state.live = {
        content: "",
        reasoning: "",
        tools: [],
        round: 0,
        timelineReady: false,
        reasoningEl: null,
        reasoningBlock: null,
        reasoningBlocks: [],
        currentReasoningBlock: null,
        currentTextBlock: null,
        view: {
          root,
          body: root.querySelector(".message-content"),
          reasoning: root.querySelector(".reasoning"),
          tools: root.querySelector(".tool-events"),
        },
      };
    }
    return state.live;
  }

  function updateSendButton() {
    sendButton.disabled = false;
    if (state.running) {
      sendButton.classList.add("stop");
      sendButton.querySelector(".send-label").textContent = "停止";
      sendButton.querySelector(".send-arrow").textContent = "■";
      // type=button so Enter in the textarea cannot double-submit;
      // stop is handled by the explicit click listener below.
      sendButton.type = "button";
    } else {
      sendButton.classList.remove("stop");
      sendButton.querySelector(".send-label").textContent = "发送";
      sendButton.querySelector(".send-arrow").textContent = "↑";
      sendButton.type = "submit";
    }
  }

  // When running, the button is type=button and will not fire form submit.
  sendButton.addEventListener("click", event => {
    if (!state.running) return;
    event.preventDefault();
    event.stopPropagation();
    state.controller?.abort();
    stopActiveRun();
  });

  composer.addEventListener("submit", event => {
    event.preventDefault();
    if (state.running) {
      // Fallback if submit still fires while running.
      state.controller?.abort();
      stopActiveRun();
      return;
    }
    const value = promptEl.value;
    promptEl.value = "";
    autoResize();
    updateCharCount();
    send(value);
  });

  async function loadSession(keepMessages = false) {
    try {
      const response = await fetch(`/api/session?session_id=${encodeURIComponent(state.sessionId)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      state.sessionId = data.session_id;
      localStorage.setItem("spark_session_id", state.sessionId);
      const topTitle = $("#topTitle");
      if (topTitle) topTitle.textContent = data.title || "新对话";
      if (!keepMessages) {
        state.messages = data.messages || [];
        state.plan = data.plan || [];
        renderSavedMessages();
        renderPlan();
      }
      setWorkspaceDisplay(data.workspace);
      if (data.updated_at) {
        const saved = new Date(data.updated_at);
        const stamp = Number.isNaN(saved.getTime())
          ? data.updated_at
          : saved.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        setSaveStatus(`已保存 · ${stamp}`);
      }
      return data.plan || [];
    } catch (error) {
      showToast("无法加载会话：" + error.message);
      return [];
    }
  }

  /** Fill settings-modal「运行健康」+ about tab from a /api/health payload. */
  function renderSettingsHealth(data) {
    const health = data?.model_health || {};
    const mf = data?.model_file || health.model_file || {};
    const detail = document.getElementById("healthDetail");
    if (detail) {
      detail.textContent =
        `上下文 ${Number(health.context_used || 0).toLocaleString()} / ${Number(health.context_total || 0).toLocaleString()} · ` +
        `${health.backend_profile || "auto"} · GPU ${health.settings?.gpu_layers || "auto"} · ` +
        `模型 ${mf.name || data?.model || "—"}`;
    }
    const live = document.getElementById("liveModel");
    if (live) {
      const bits = [];
      if (mf.quant) bits.push(mf.quant);
      if (mf.size_human && mf.size_human !== "—") bits.push(mf.size_human);
      bits.push(health.model_healthy && health.llama_healthy ? "Healthy" : "异常");
      live.textContent = `运行中：${mf.name || data?.model || "—"} · ${bits.join(" · ")}`;
      live.title = mf.path || "";
    }
  }

  async function health() {
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      const data = await response.json();
      state.health = data;
      state.healthAt = Date.now();
      setWorkspaceDisplay(data.workspace);
      renderHealth(data);
      renderSettingsHealth(data);
    } catch {
      $("#statusDot").className = "status-dot offline";
      $("#statusText").textContent = "Web 服务异常";
      const badge = $("#modelHealthBadge");
      if (badge) {
        badge.textContent = "ERROR";
        badge.className = "health-badge bad";
      }
    }
  }

  promptEl.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (state.running) return;
      composer.requestSubmit();
    }
  });

  function autoResize() {
    promptEl.style.height = "auto";
    promptEl.style.height = `${Math.min(promptEl.scrollHeight, 220)}px`;
  }
  function updateCharCount() {
    $("#charCount").textContent = String(promptEl.value.length);
  }
  promptEl.addEventListener("input", () => { autoResize(); updateCharCount(); });

  $("#attachButton").addEventListener("click", () => $("#attachmentInput").click());
  $("#attachmentInput").addEventListener("change", async event => {
    await addAttachments([...event.target.files]);
    event.target.value = "";
  });

  document.querySelectorAll("[data-prompt]").forEach(button => {
    button.addEventListener("click", () => send(button.dataset.prompt));
  });

  $("#newChat").addEventListener("click", async () => {
    if (state.running) {
      state.controller?.abort();
      await stopActiveRun();
      state.running = false;
      state.controller = null;
      updateSendButton();
    }
    state.sessionId = randomId();
    localStorage.setItem("spark_session_id", state.sessionId);
    state.messages = [];
    state.plan = [];
    state.live = null;
    state.loopStep = 0;
    state.attachments = [];
    const topTitle = $("#topTitle");
    if (topTitle) topTitle.textContent = "新对话";
    renderAttachments();
    setSaveStatus("自动保存已开启");
    setLoop("等待任务…", 0, "");
    renderSavedMessages();
    renderPlan();
    await loadSession();
    await loadConversationList();
    sidebar.classList.remove("open");
    promptEl.focus();
  });

  const saveSettingsButton = $("#saveSettings");
  if (saveSettingsButton) saveSettingsButton.addEventListener("click", saveSettings);
  $("#switchWorkspace").addEventListener("click", switchWorkspace);
  $("#workspaceInput").addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      switchWorkspace();
    }
  });

  $("#sidebarToggle").addEventListener("click", () => sidebar.classList.toggle("open"));
  messagesEl.addEventListener("click", event => {
    if (window.innerWidth <= 800 && sidebar.classList.contains("open") && !sidebar.contains(event.target)) {
      sidebar.classList.remove("open");
    }
  });

  async function loadAboutTab() {
    const badge = document.getElementById("healthBadge");
    const paint = (data) => {
      const healthH = data.model_health || {};
      const online = Boolean(healthH.model_healthy && healthH.llama_healthy);
      if (badge) {
        badge.textContent = online ? "模型 Healthy" : "模型异常";
        badge.className = "sp-badge" + (online ? "" : " bad");
      }
      const mf = data.model_file || healthH.model_file || {};
      const setText = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
      };
      setText("aboutModel", mf.name || data.model || "Spark");
      setText(
        "aboutContext",
        `${(healthH.context_used || 0).toLocaleString()} / ${(healthH.context_total || 0).toLocaleString()} · ${healthH.context_percent || 0}%`,
      );
      setText("aboutWorkspace", data.workspace || "未知");
      setText(
        "aboutMode",
        `${healthH.backend_profile || "auto"} · GPU ${healthH.settings?.gpu_layers || "auto"}`,
      );
      const grid = document.getElementById("aboutToolGrid");
      if (grid) {
        grid.innerHTML = "";
        for (const tool of data.tools || []) {
          const chip = document.createElement("span");
          chip.className = "sp-chip";
          chip.textContent = tool;
          grid.appendChild(chip);
        }
      }
    };

    // Instant paint from last health() if we have it (< ~8s old).
    if (state.health && Date.now() - (state.healthAt || 0) < 8000) {
      paint(state.health);
    }
    try {
      const data = await fetch("/api/health", { cache: "no-store" }).then((r) => r.json());
      state.health = data;
      state.healthAt = Date.now();
      paint(data);
      renderSettingsHealth(data);
    } catch {
      if (badge && !(state.health && Date.now() - (state.healthAt || 0) < 8000)) {
        badge.textContent = "Web 服务异常";
        badge.className = "sp-badge bad";
      }
    }
  }

  function openAppModal(tab) {
    const root = document.getElementById("appModal");
    if (!root) return;
    root.hidden = false;
    document.body.style.overflow = "hidden";
    const which = tab || "settings";
    // switchModalTab also loads about / paints settings health.
    switchModalTab(which);
    if (state.health) renderSettingsHealth(state.health);
    health().catch(() => {});
    if (which === "settings") loadSettings();
  }

  function closeAppModal() {
    const root = document.getElementById("appModal");
    if (!root) return;
    root.hidden = true;
    document.body.style.overflow = "";
  }

  function switchModalTab(name) {
    const tabs = document.querySelectorAll(".modal-tab");
    const panels = document.querySelectorAll(".modal-panel");
    tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    panels.forEach((p) => {
      p.hidden = p.dataset.panel !== name;
    });
    if (name === "about") loadAboutTab();
    else if (name === "settings" && state.health) renderSettingsHealth(state.health);
  }

  function bindAppModal() {
    const root = document.getElementById("appModal");
    if (!root) return;
    // Own control library: upgrade native selects / switches / steppers.
    if (window.SP) SP.enhanceAll(root);
    document.getElementById("openSettings")?.addEventListener("click", (e) => {
      e.preventDefault();
      openAppModal("settings");
    });
    document.getElementById("stopLlama")?.addEventListener("click", async () => {
      if (state.running) {
        showToast("请先停止当前任务，再操作模型服务");
        return;
      }
      const btn = document.getElementById("stopLlama");
      if (!btn || btn.dataset.busy === "1") return;
      const mode = btn.dataset.mode === "start" ? "start" : "stop";

      if (mode === "stop") {
        const yes = window.confirm(
          "手动停止主模型？\n\n停止后不会自动重启，按钮会变成「启动模型」。",
        );
        if (!yes) return;
      }

      btn.dataset.busy = "1";
      btn.disabled = true;
      btn.textContent = mode === "stop" ? "停止中…" : "启动中…";

      try {
        const response = await fetch(mode === "stop" ? "/api/llama/stop" : "/api/llama/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        showToast(data.message || (mode === "stop" ? "已停止模型" : "已启动模型"));
        // Refresh health a few times while the model loads.
        for (let i = 0; i < 8; i += 1) {
          // eslint-disable-next-line no-await-in-loop
          await health();
          // eslint-disable-next-line no-await-in-loop
          await new Promise((r) => setTimeout(r, mode === "start" ? 1500 : 400));
          const h = state.health?.model_health || {};
          if (mode === "stop" && !(h.model_healthy && h.llama_healthy)) break;
          if (mode === "start" && h.model_healthy && h.llama_healthy) break;
        }
        await health();
      } catch (error) {
        showToast((mode === "stop" ? "停止失败：" : "启动失败：") + (error.message || "未知错误"));
        await health().catch(() => {});
      } finally {
        btn.dataset.busy = "0";
        btn.disabled = false;
        const h = state.health?.model_health || {};
        const online = Boolean(h.model_healthy && h.llama_healthy);
        const manual = Boolean(state.health?.llama_manual_stopped || h.llama_manual_stopped);
        updateLlamaToggle(online, manual);
      }
    });
    root.querySelectorAll("[data-close-modal]").forEach((el) => {
      el.addEventListener("click", closeAppModal);
    });
    root.querySelectorAll(".modal-tab").forEach((tab) => {
      tab.addEventListener("click", () => switchModalTab(tab.dataset.tab));
    });
    document.getElementById("settingModel")?.addEventListener("change", () => {
      const sel = document.getElementById("settingModel");
      const opt = sel.selectedOptions[0];
      if (opt && sel.value) {
        const models = state._modelsCache || [];
        const full = models.find((m) => m.path === sel.value);
        if (full) {
          renderModelInfo(full, null);
        } else {
          renderModelInfo(
            {
              name: (opt.textContent || "").split("·")[0].trim(),
              path: sel.value,
              size_bytes: 0,
              size_human: opt.dataset.size || "",
              quant: opt.dataset.quant || "",
              vision: opt.dataset.vision === "1",
              mtp: opt.dataset.mtp === "1",
              diffusion: opt.dataset.diffusion === "1",
              mmproj: opt.dataset.mmproj || "",
              text: opt.dataset.vision !== "1" && opt.dataset.mtp !== "1"
                ? true
                : true,
              error: null,
            },
            null,
          );
        }
        if (window.SP) SP.refresh(sel);
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !root.hidden) closeAppModal();
    });
  }

  async function bootstrap() {
    updateSendButton();
    updateCharCount();
    bindAppModal();
    // Show model health ASAP — don't wait for session load.
    health().catch(() => {});
    await Promise.all([loadSession(), loadConversationList()]);
    health().catch(() => {});
    setInterval(health, 15000);
    // Reattach if this session still has a background agent run.
    await resumeRunningSession();
  }

  bootstrap();
})();
