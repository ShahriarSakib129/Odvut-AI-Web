/*
 * Safe markdown -> DOM renderer for AI answers.
 *
 * Output is built only with document.createElement / createTextNode. Nothing is
 * ever parsed as HTML, so model output (or text an admin imported) cannot inject
 * markup or script. Links are limited to http, https and mailto and open with
 * rel="noopener noreferrer nofollow".
 *
 * Supported: paragraphs, #/##/### headings, **bold**, *italic*, `code`, fenced
 * code blocks, bullet and numbered lists, > quotes, horizontal rules,
 * [text](url) links and bare http(s) URLs.
 */
(function (root) {
  "use strict";

  var SAFE_HREF = /^(https?:\/\/|mailto:)/i;
  var INLINE = /`([^`\n]+)`|\*\*([^*\n]+?)\*\*|__([^_\n]+?)__|\*([^*\n]+?)\*|(^|[^\w])_([^_\n]+?)_(?!\w)|\[([^\]\n]{1,200})\]\(([^()\s]+)\)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

  function el(doc, tag, attrs, children) {
    var node = doc.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    }
    (children || []).forEach(function (child) {
      if (child == null) return;
      node.appendChild(typeof child === "string" ? doc.createTextNode(child) : child);
    });
    return node;
  }

  function safeHref(href) {
    if (typeof href !== "string" || !SAFE_HREF.test(href) || href.length > 2000) return null;
    return href;
  }

  // inline formatting: returns an array of DOM nodes / strings
  function inline(doc, text, depth) {
    var out = [];
    var last = 0;
    var m;
    // a fresh RegExp per call: recursion must not share lastIndex with the caller
    var re = new RegExp(INLINE.source, "g");
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) out.push(text.slice(last, m.index));
      if (m[1] !== undefined) {
        out.push(el(doc, "code", null, [m[1]]));
      } else if ((m[2] !== undefined || m[3] !== undefined) && depth < 2) {
        out.push(el(doc, "strong", null, inline(doc, m[2] !== undefined ? m[2] : m[3], depth + 1)));
      } else if (m[4] !== undefined && depth < 2) {
        out.push(el(doc, "em", null, inline(doc, m[4], depth + 1)));
      } else if (m[6] !== undefined && depth < 2) {
        out.push(m[5] || "");
        out.push(el(doc, "em", null, inline(doc, m[6], depth + 1)));
      } else if (m[7] !== undefined && m[8] !== undefined) {
        var href = safeHref(m[8]);
        if (href) {
          out.push(el(doc, "a", { href: href, target: "_blank", rel: "noopener noreferrer nofollow" },
                      [m[7]]));
        } else {
          out.push(m[0]);
        }
      } else if (m[9] !== undefined) {
        out.push(el(doc, "a", { href: m[9], target: "_blank", rel: "noopener noreferrer nofollow" }, [m[9]]));
      } else {
        out.push(m[0]);
      }
      last = re.lastIndex;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  function withBreaks(doc, text) {
    var parts = text.split("\n");
    var nodes = [];
    parts.forEach(function (line, i) {
      if (i > 0) nodes.push(el(doc, "br"));
      inline(doc, line, 0).forEach(function (n) { nodes.push(n); });
    });
    return nodes;
  }

  function blocks(doc, source, depth) {
    var lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    var out = [];
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      var fence = /^\s*```\s*([A-Za-z0-9_+-]{0,20})?\s*$/.exec(line);
      if (fence) {
        var code = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { code.push(lines[i]); i++; }
        i++; // skip closing fence (or end of input)
        var codeEl = el(doc, "code", null, [code.join("\n")]);
        if (fence[1]) codeEl.setAttribute("data-lang", fence[1]);
        out.push(el(doc, "pre", null, [codeEl]));
        continue;
      }
      if (/^\s*$/.test(line)) { i++; continue; }
      var heading = /^\s*(#{1,3})\s+(.+?)\s*#*\s*$/.exec(line);
      if (heading) {
        var tag = heading[1].length === 1 ? "h3" : (heading[1].length === 2 ? "h3" : "h4");
        out.push(el(doc, tag, null, inline(doc, heading[2], 0)));
        i++;
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
        out.push(el(doc, "hr"));
        i++;
        continue;
      }
      if (/^\s*>/.test(line)) {
        var quote = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*>\s?/, ""));
          i++;
        }
        out.push(el(doc, "blockquote", null, depth < 3 ? blocks(doc, quote.join("\n"), depth + 1)
                                                        : [quote.join(" ")]));
        continue;
      }
      if (/^\s*([-*•])\s+/.test(line) || /^\s*\d{1,3}[.)]\s+/.test(line)) {
        var ordered = /^\s*\d/.test(line);
        var items = [];
        var itemRe = ordered ? /^\s*\d{1,3}[.)]\s+(.*)$/ : /^\s*[-*•]\s+(.*)$/;
        while (i < lines.length && itemRe.test(lines[i])) {
          var im = itemRe.exec(lines[i]);
          items.push(el(doc, "li", null, inline(doc, im[1], 0)));
          i++;
        }
        out.push(el(doc, ordered ? "ol" : "ul", null, items));
        continue;
      }
      var para = [];
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^\s*(```|#{1,3}\s|>|[-*•]\s|\d{1,3}[.)]\s)/.test(lines[i])) {
        para.push(lines[i]);
        i++;
      }
      if (para.length === 0) { para.push(lines[i]); i++; }
      out.push(el(doc, "p", null, withBreaks(doc, para.join("\n"))));
    }
    return out;
  }

  function renderInto(doc, container, text) {
    while (container.firstChild) container.removeChild(container.firstChild);
    blocks(doc, text, 0).forEach(function (node) { container.appendChild(node); });
    container.classList && container.classList.add("md");
    return container;
  }

  var api = { renderInto: renderInto, _blocks: blocks, _inline: inline };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.SafeMarkdown = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
