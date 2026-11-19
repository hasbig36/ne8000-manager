(function () {
  "use strict";
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const usd = (v) => "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const kes = (v) => "KES " + Math.ceil(v).toLocaleString("en-US");

  // ---- pricing calculator on the home page (prices worked out here, same rules as the server)
  const calc = $("#calc");
  if (calc) {
    const first = +calc.dataset.first, extra = +calc.dataset.extra, rate = +calc.dataset.kes;
    const sizes = JSON.parse(calc.dataset.sizes);
    const n = $("#n", calc);
    const out = (k, v) => { const el = $(`[data-out="${k}"]`, calc); if (el) el.textContent = v; };
    const update = () => {
      let d = Math.max(1, Math.min(200, parseInt(n.value, 10) || 1));
      const hosted = $('input[name="h"]:checked', calc).value === "hosted";
      const size = sizes.find((s) => d <= s.max) || sizes[sizes.length - 1];
      const lic = first + extra * (d - 1);
      const host = hosted ? size.hosted : 0;
      out("n", d); out("s", d > 1 ? "s" : "");
      out("lic", usd(lic)); out("host", usd(host)); out("total", usd(lic + host));
      out("kes", kes((lic + host) * rate));
      out("server", size.label);
      $("[data-hosted]", calc).classList.toggle("hidden", !hosted);
      $("#calcBuy").href = `/buy?devices=${d}&hosting=${hosted ? "hosted" : "self"}`;
    };
    $$("button[data-step]", calc).forEach((b) => b.addEventListener("click", () => {
      n.value = Math.max(1, Math.min(200, (parseInt(n.value, 10) || 1) + +b.dataset.step)); update();
    }));
    n.addEventListener("input", update);
    $$('input[name="h"]', calc).forEach((r) => r.addEventListener("change", update));
    update();
  }

  // ---- checkout: live quote from the server
  const form = $("#buyForm");
  if (form) {
    const qs = new URLSearchParams(location.search);
    const firstDev = $("input[data-dev]", form);
    if (qs.get("devices") && firstDev) firstDev.value = Math.max(1, Math.min(200, parseInt(qs.get("devices"), 10) || 1));
    if (qs.get("hosting") === "hosted") { const h = $('input[name="hosting"][value="hosted"]', form); if (h && !h.disabled) h.checked = true; }
    const q = (k) => $(`[data-q="${k}"]`, form);
    let timer = 0;
    const refresh = () => {
      const hosting = $('input[name="hosting"]:checked', form).value;
      $$("[data-help]", form).forEach((p) => p.classList.toggle("hidden", p.dataset.help !== hosting));
      const devices = $$("input[data-dev]", form).map((i) => ({ type: i.dataset.dev, count: parseInt(i.value, 10) || 0 }));
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const r = await fetch("/api/quote", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ devices, hosting }) });
        const d = await r.json();
        q("error").classList.toggle("hidden", !d.error);
        if (d.error) { q("error").textContent = d.error; return; }
        q("devices").textContent = d.devices;
        q("licence_usd").textContent = usd(d.licence_usd);
        q("hosting_usd").textContent = usd(d.hosting_usd);
        q("total_usd").textContent = usd(d.total_usd);
        q("total_kes").textContent = "KES " + d.total_kes.toLocaleString("en-US");
        q("server").textContent = d.server.label;
        q("server_note").textContent = hosting === "hosted" ? `Hosted: ${usd(d.server.hosted_monthly)} per month` : "Use a server at least this size";
        $("[data-q-hosted]", form).classList.toggle("hidden", hosting !== "hosted");
      }, 150);
    };
    form.addEventListener("input", refresh);
    form.addEventListener("change", refresh);
    refresh();
  }

  // ---- waiting for the M-Pesa prompt
  const wait = $("#wait");
  if (wait) {
    const started = Date.now();
    const msg = $('[data-w="msg"]', wait);
    const poll = async () => {
      try {
        const r = await fetch(`/api/payment/${encodeURIComponent(wait.dataset.checkout)}`);
        const d = await r.json();
        if (d.status === "paid") { location.href = `/pay/${wait.dataset.ref}`; return; }
        if (d.status === "failed") {
          $('[data-w="spin"]', wait).classList.add("hidden");
          msg.textContent = (d.note || "The payment did not go through.") + " You can try again from the invoice.";
          return;
        }
      } catch (e) { /* keep waiting */ }
      if (Date.now() - started > 3 * 60 * 1000) {
        $('[data-w="spin"]', wait).classList.add("hidden");
        msg.textContent = "We have not heard from M-Pesa yet. If you paid, the invoice updates within a few minutes.";
        return;
      }
      setTimeout(poll, 5000);
    };
    setTimeout(poll, 8000);
  }
})();
