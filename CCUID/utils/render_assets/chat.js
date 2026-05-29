// quadrantChart 自动修补：mermaid 11.x 对 x-axis / y-axis / quadrant-N / data
// point label 要求加双引号（含中文/非 ASCII 时），用户/LLM 写源经常不加。
// 在交给 mermaid.render 之前给这些位置自动包引号；已加引号的跳过。
// title 实测可以不加引号（中文括号也容忍）。
function patchMermaid(source) {
  const lines = source.split('\n');
  let inQuadrant = false;
  for (let i = 0; i < lines.length; i += 1) {
    const trimmed = lines[i].trim();
    if (trimmed === 'quadrantChart' || trimmed.startsWith('quadrantChart ')) {
      inQuadrant = true;
      continue;
    }
    if (!inQuadrant) continue;
    // x-axis / y-axis: 两端 label 用 --> 分隔
    const axis = lines[i].match(/^(\s*)([xy]-axis)\s+(.+?)\s*-->\s*(.+?)\s*$/);
    if (axis) {
      const [, indent, kw, left, right] = axis;
      lines[i] = `${indent}${kw} ${quote(left)} --> ${quote(right)}`;
      continue;
    }
    // quadrant-N: 后面单个 label
    const quad = lines[i].match(/^(\s*)(quadrant-[1-4])\s+(.+?)\s*$/);
    if (quad) {
      const [, indent, kw, label] = quad;
      lines[i] = `${indent}${kw} ${quote(label)}`;
      continue;
    }
    // data point: `<key>: [x, y]` —— key 含非 ASCII 时必须引号
    const dp = lines[i].match(/^(\s*)([^"\s][^:]*?):\s*(\[[^\]]+\])\s*$/);
    if (dp) {
      const [, indent, key, coords] = dp;
      lines[i] = `${indent}${quote(key)}: ${coords}`;
    }
  }
  return lines.join('\n');
}

function quote(label) {
  const t = label.trim();
  if (t.startsWith('"') && t.endsWith('"')) return t;
  return `"${t.replace(/"/g, '')}"`;
}

window.ccuidRenderExtras = async () => {
  // dollarmath plugin 输出：行内 <span class="math inline">latex</span>，
  // 块 <div class="math block">latex</div>。内容已是裸 latex，无 \(...\) 包裹。
  for (const el of document.querySelectorAll('.math')) {
    const displayMode = el.classList.contains('block');
    try {
      window.katex.render((el.textContent || '').trim(), el, {
        displayMode,
        throwOnError: false,
        output: 'htmlAndMathml',
      });
    } catch {
      el.classList.add('cc-math-error');
    }
  }

  if (document.querySelector('pre.mermaid')) {
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'default',
      fontFamily: 'CCUID Noto Sans SC, system-ui, sans-serif',
    });
  }

  const blocks = Array.from(document.querySelectorAll('pre.mermaid'));
  for (let i = 0; i < blocks.length; i += 1) {
    const block = blocks[i];
    const rawSource = block.textContent || '';
    const source = patchMermaid(rawSource);
    const renderId = `ccuid-mermaid-${Date.now()}-${i}`;
    try {
      const result = await window.mermaid.render(renderId, source);
      const wrapper = document.createElement('div');
      wrapper.className = 'cc-mermaid';
      wrapper.innerHTML = result.svg;
      block.replaceWith(wrapper);
    } catch {
      // 修补后仍渲染失败：降级成普通代码块（跟 cc-session 一致），caption
      // 标 "mermaid"，无警告 UI，源码完整保留。不显示炸弹卡。
      const wrapper = document.createElement('figure');
      wrapper.className = 'cc-code';
      const caption = document.createElement('figcaption');
      caption.textContent = 'mermaid';
      const pre = document.createElement('pre');
      const code = document.createElement('code');
      code.textContent = rawSource;
      pre.appendChild(code);
      wrapper.appendChild(caption);
      wrapper.appendChild(pre);
      block.replaceWith(wrapper);
      // mermaid 11.x render 抛错时副作用注入 error SVG（炸弹图标）到 body；
      // 仅在失败路径清孤儿。selector 范围限定 body 直接子节点 + 排除 wrapper，
      // 否则会误删 wrapper 里成功的 svg（svg id 等于 renderId）
      document.querySelectorAll(`[id^="d${renderId}"], #${renderId}`).forEach((n) => {
        if (!n.closest('.cc-mermaid')) n.remove();
      });
    }
  }
  // 兜底：清掉 mermaid 留下的孤儿 error svg
  document.querySelectorAll('svg[aria-roledescription="error"]').forEach((n) => {
    const parent = n.parentElement;
    if (!parent || !parent.classList.contains('cc-mermaid')) n.remove();
  });
};
