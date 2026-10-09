// Run with: node --test tests/js/
// Uses a minimal DOM stand-in so the renderer's output tree can be asserted without a browser.
const test = require("node:test");
const assert = require("node:assert/strict");
const md = require("../../web/static/js/markdown.js");

class FakeNode {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.parentNode = null; this.classList = { add() {} }; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  get firstChild() { return this.children[0] || null; }
  get textContent() { return this.children.map((c) => (c.isText ? c.data : c.textContent)).join(""); }
}
class FakeText { constructor(d) { this.data = d; this.isText = true; } get textContent() { return this.data; } }
const doc = {
  createElement: (t) => new FakeNode(t.toLowerCase()),
  createTextNode: (d) => new FakeText(d),
};
function render(text) {
  const root = new FakeNode("div");
  md.renderInto(doc, root, text);
  return root;
}
function tags(node) {
  return node.children.map((c) => (c.isText ? "#text" : c.tagName));
}

test("paragraphs and line breaks", () => {
  const r = render("first line\nsecond line\n\nnext paragraph");
  assert.deepEqual(tags(r), ["p", "p"]);
  assert.deepEqual(tags(r.children[0]), ["#text", "br", "#text"]);
});

test("bold, italic and inline code", () => {
  const r = render("**bold** and *it* and `x = 1`");
  const p = r.children[0];
  assert.deepEqual(tags(p), ["strong", "#text", "em", "#text", "code"]);
  assert.equal(p.children[4].textContent, "x = 1");
});

test("lists and headings", () => {
  const r = render("## Title\n- one\n- two\n\n1. first\n2. second");
  assert.deepEqual(tags(r), ["h3", "ul", "ol"]);
  assert.equal(r.children[1].children.length, 2);
  assert.equal(r.children[2].children.length, 2);
});

test("fenced code keeps content literal", () => {
  const r = render("```js\n<script>alert(1)</script>\n```");
  const pre = r.children[0];
  assert.equal(pre.tagName, "pre");
  assert.equal(pre.children[0].textContent, "<script>alert(1)</script>");
  assert.equal(pre.children[0].attrs["data-lang"], "js");
});

test("raw html in text is never turned into elements", () => {
  const r = render("<img src=x onerror=alert(1)> <b>hi</b>");
  const flat = JSON.stringify(r, (k, v) => (k === "parentNode" ? undefined : v));
  assert.ok(!flat.includes('"tagName":"img"'));
  assert.ok(!flat.includes('"tagName":"b"'));
  assert.equal(r.children[0].textContent, "<img src=x onerror=alert(1)> <b>hi</b>");
});

test("only http, https and mailto links become anchors", () => {
  const r = render("[ok](https://example.com/a) [bad](javascript:alert(1)) [mail](mailto:a@b.co)");
  const links = r.children[0].children.filter((c) => c.tagName === "a");
  assert.equal(links.length, 2);
  assert.equal(links[0].attrs.href, "https://example.com/a");
  assert.equal(links[0].attrs.rel, "noopener noreferrer nofollow");
  // the javascript: link stays as literal text
  assert.ok(r.children[0].textContent.includes("[bad](javascript:alert(1))"));
});

test("bare URLs are linked without the trailing punctuation", () => {
  const r = render("See https://example.com/docs.");
  const a = r.children[0].children.find((c) => c.tagName === "a");
  assert.equal(a.attrs.href, "https://example.com/docs");
});

test("blockquote and horizontal rule", () => {
  const r = render("> quoted **text**\n\n---");
  assert.deepEqual(tags(r), ["blockquote", "hr"]);
});

test("re-rendering replaces previous content", () => {
  const root = new FakeNode("div");
  md.renderInto(doc, root, "one");
  md.renderInto(doc, root, "two");
  assert.equal(root.children.length, 1);
  assert.equal(root.textContent, "two");
});
