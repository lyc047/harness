/**
 * Zero-dependency, escape-first markdown renderer.
 *
 * Security model: every dynamic text value is HTML-escaped BEFORE any
 * rendering. The inline renderer only builds anchors from URLs that pass
 * safeUrl() (http/https). Everything else stays literal text.
 */
(function (global) {
  'use strict';

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /** Only http/https URLs are allowed; everything else renders as plain text. */
  function safeUrl(url) {
    try {
      const u = new URL(url.trim(), global.location.origin);
      if (u.protocol === 'http:' || u.protocol === 'https:') return u.href;
    } catch (e) { /* not a URL */ }
    return null;
  }

  function renderInline(text) {
    // [label](url)
    text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (m, label, url) {
      const safe = safeUrl(url);
      if (!safe) return escapeHtml(label);
      return '<a href="' + safe + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(label) + '</a>';
    });
    // inline code
    text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
    // bold
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // italic
    text = text.replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, '$1<em>$2</em>');
    // strikethrough
    text = text.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    return text;
  }

  function renderBlock(src) {
    const lines = String(src || '').split('\n');
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      // fenced code block
      const fence = line.match(/^```([\w+-]*)\s*$/);
      if (fence) {
        const lang = fence[1];
        const code = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) {
          code.push(lines[i]);
          i++;
        }
        i++; // skip closing fence (or EOF)
        out.push(
          '<pre><code' +
            (lang ? ' class="lang-' + escapeHtml(lang) + '"' : '') +
            '>' + escapeHtml(code.join('\n')) + '</code></pre>'
        );
        continue;
      }

      // heading
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        const level = h[1].length;
        out.push('<h' + level + '>' + renderInline(escapeHtml(h[2])) + '</h' + level + '>');
        i++;
        continue;
      }

      // horizontal rule
      if (/^\s*([-*_])\s*\1\s*\1\s*$/.test(line)) {
        out.push('<hr>');
        i++;
        continue;
      }

      // blockquote
      if (/^\s*>\s?/.test(line)) {
        const quote = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*>\s?/, ''));
          i++;
        }
        out.push('<blockquote>' + renderInline(escapeHtml(quote.join('\n'))) + '</blockquote>');
        continue;
      }

      // list (bullet or ordered)
      const li = line.match(/^\s*([-*+]|\d+\.)\s+(.*)$/);
      if (li) {
        const ordered = /\d+\./.test(li[1]);
        const items = [];
        while (i < lines.length) {
          const m = lines[i].match(/^\s*([-*+]|\d+\.)\s+(.*)$/);
          if (!m) break;
          items.push('<li>' + renderInline(escapeHtml(m[2])) + '</li>');
          i++;
        }
        out.push('<' + (ordered ? 'ol' : 'ul') + '>' + items.join('') + '</' + (ordered ? 'ol' : 'ul') + '>');
        continue;
      }

      // paragraph
      const para = [];
      while (
        i < lines.length &&
        lines[i].trim() !== '' &&
        !/^```/.test(lines[i]) &&
        !/^#{1,6}\s/.test(lines[i]) &&
        !/^\s*([-*_])\s*\1\s*\1\s*$/.test(lines[i]) &&
        !/^\s*>\s?/.test(lines[i]) &&
        !/^\s*([-*+]|\d+\.)\s+/.test(lines[i])
      ) {
        para.push(lines[i]);
        i++;
      }
      if (para.length) {
        out.push('<p>' + renderInline(escapeHtml(para.join(' '))) + '</p>');
        continue;
      }
      i++; // blank line or unrecognized token
    }

    return out.join('\n');
  }

  global.HarnessMarkdown = {
    escapeHtml: escapeHtml,
    safeUrl: safeUrl,
    renderMarkdown: renderBlock,
    renderInline: renderInline,
  };
})(window);
