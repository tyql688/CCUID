window.ccuidRenderExtras = async () => {
  for (const el of document.querySelectorAll(".arithmatex")) {
    let tex = (el.textContent || "").trim();
    let displayMode = false;
    if (tex.startsWith("\\(") && tex.endsWith("\\)")) {
      tex = tex.slice(2, -2);
    } else if (tex.startsWith("\\[") && tex.endsWith("\\]")) {
      tex = tex.slice(2, -2);
      displayMode = true;
    }
    try {
      window.katex.render(tex, el, {
        displayMode,
        throwOnError: false,
        output: "htmlAndMathml",
      });
    } catch {
      el.classList.add("cc-math-error");
    }
  }

  if (document.querySelector("pre.mermaid")) {
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "loose",
      theme: "default",
      fontFamily: "CCUID Noto Sans SC, system-ui, sans-serif",
    });
  }

  const blocks = Array.from(document.querySelectorAll("pre.mermaid"));
  for (let i = 0; i < blocks.length; i += 1) {
    const block = blocks[i];
    const source = block.textContent || "";
    try {
      const result = await window.mermaid.render(`ccuid-mermaid-${Date.now()}-${i}`, source);
      const wrapper = document.createElement("div");
      wrapper.className = "cc-mermaid";
      wrapper.innerHTML = result.svg;
      block.replaceWith(wrapper);
    } catch {
      block.classList.add("cc-mermaid-error");
    }
  }
};
