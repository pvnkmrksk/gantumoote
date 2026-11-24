// SPDX-License-Identifier: AGPL-3.0-or-later
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(process.argv[2] || path.join(__dirname, "assets/ui.js"), "utf8");
const start = source.indexOf("function applyPadding(");
const end = source.indexOf("let tapTimer", start);
assert.ok(start >= 0 && end > start);

let hidden = false;
const camera = [];
const insets = [];
const button = {setAttribute() {}, focus() {}};
const state = {
    immersive: () => hidden,
    chromePadding: () => hidden ? {bottom: 24} : {bottom: 80},
    map: {
        easeTo: () => camera.push("animation"),
        setPadding: () => camera.push("padding"),
    },
    document: {
        body: {classList: {toggle: (_, value) => { hidden = value; }}},
        documentElement: {style: {setProperty: (_, value) => insets.push(value)}},
        querySelectorAll: () => [],
    },
    $: () => button,
    setSidebar() {},
    toast() {},
    sessionStorage: {getItem: () => true},
};
vm.runInNewContext(source.slice(start, end) + "\napplyPadding(false); setImmersive(true); setImmersive(false);", state);
assert.deepEqual(camera, ["padding"], "Showing and hiding controls leaves the map camera alone");
assert.deepEqual(insets, ["74px", "0px", "74px"], "Floating controls still follow the current layout");
console.log("Fullscreen controls preserved camera padding.");
