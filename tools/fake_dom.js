// Подделка страницы для проверок без браузера: дерево узлов, атрибуты, события, простые селекторы ([attr], [attr="v"], .класс, #id, тег)
// и разбор настоящей разметки (parseHtml) — так проверяются реальные preview.html + preview.js вместе, включая забытые id.
"use strict";
class FakeNode {
  constructor(tag, doc) {
    this.tagName = String(tag).toLowerCase(); this.ownerDocument = doc; this.attrs = {}; this.children = []; this.parentNode = null;
    this.listeners = {}; this.className = ""; this._text = ""; this.value = ""; this.checked = false;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "class") this.className = String(v); }
  getAttribute(k) { if (k === "class") return this.className || null; return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return k in this.attrs; }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i !== -1) this.children.splice(i, 1); c.parentNode = null; return c; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  set textContent(v) { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  dispatch(type, extra) { (this.listeners[type] || []).slice().forEach((fn) => fn(Object.assign({ type, target: this }, extra))); }
  click() { this.dispatch("click"); }
  matches(sel) {
    let m;
    if ((m = /^\[([\w-]+)\]$/.exec(sel))) return m[1] in this.attrs || (m[1] === "class" && !!this.className);
    if ((m = /^\[([\w-]+)="([^"]*)"\]$/.exec(sel))) return this.getAttribute(m[1]) === m[2];
    if ((m = /^\.([\w-]+)$/.exec(sel))) return this.className.split(/\s+/).includes(m[1]);
    if ((m = /^#([\w-]+)$/.exec(sel))) return this.attrs.id === m[1];
    return this.tagName === sel.toLowerCase();
  }
  all(pred, out) { out = out || []; this.children.forEach((c) => { if (pred(c)) out.push(c); c.all(pred, out); }); return out; }
  querySelectorAll(sel) { return this.all((n) => n.tagName !== "#text" && n.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  byClass(name) { return this.all((n) => n.tagName !== "#text" && n.matches("." + name)); }
  byTag(name) { return this.all((n) => n.tagName === name); }
  focus() { this.ownerDocument.activeElement = this; }
}
class FakeDocument {
  constructor() { this.documentElement = new FakeNode("html", this); this.body = this.documentElement.appendChild(new FakeNode("body", this)); this.title = ""; this.activeElement = this.body; }
  createElement(tag) { return new FakeNode(tag, this); }
  createElementNS(ns, tag) { const n = new FakeNode(tag, this); n.namespace = ns; return n; }
  createTextNode(s) { const n = new FakeNode("#text", this); n._text = String(s); return n; }
  getElementById(id) { return this.documentElement.querySelector("#" + id); }
  querySelectorAll(sel) { return this.documentElement.querySelectorAll(sel); }
  querySelector(sel) { return this.documentElement.querySelector(sel); }
}
const VOID = new Set(["meta", "link", "input", "br", "img", "hr"]);
/** Разбор разметки страницы в дерево: достаточно для наших страниц (теги, атрибуты, текст); <head>, <script>, <style> пропускаются, <title> учитывается. */
function parseHtml(doc, html) {
  const stack = [doc.body];
  const clean = html.replace(/<!doctype[^>]*>/i, "").replace(/<!--[\s\S]*?-->/g, "").replace(/<head>[\s\S]*?<\/head>/i, "").replace(/<(script|style)[\s\S]*?<\/\1>/gi, "");
  const re = /<\/?([a-zA-Z][\w-]*)([^>]*)>|([^<]+)/g;
  let m;
  while ((m = re.exec(clean))) {
    if (m[3] !== undefined) { if (m[3].trim()) stack[stack.length - 1].appendChild(doc.createTextNode(m[3].replace(/\s+/g, " "))); continue; }
    const tag = m[1].toLowerCase();
    if (m[0].startsWith("</")) { if (!VOID.has(tag) && stack.length > 1) stack.pop(); continue; }
    if (tag === "html" || tag === "body") continue;
    const node = doc.createElement(tag);
    const attrRe = /([\w-]+)(?:="([^"]*)")?/g; let a;
    while ((a = attrRe.exec(m[2]))) node.setAttribute(a[1], a[2] === undefined ? "" : a[2].replace(/&amp;/g, "&"));
    stack[stack.length - 1].appendChild(node);
    if (!VOID.has(tag) && !m[0].endsWith("/>")) stack.push(node);
  }
  return doc;
}
function makeStorage(initial) {
  const m = new Map(Object.entries(initial || {}));
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => { m.set(k, String(v)); }, removeItem: (k) => { m.delete(k); }, _m: m };
}
module.exports = { FakeNode, FakeDocument, parseHtml, makeStorage };
