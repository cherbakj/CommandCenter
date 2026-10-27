// Preview build: stands in for Command Center's local server so the real interface can be clicked through in a
// browser. window.fetch answers every /api/* call from the made-up sample data below (every person, company,
// address, order and ticket here is invented); nothing leaves the page. WebSockets, EventSource and beacons are
// stubbed, and module stylesheets are already inlined, so their <link> tags are dropped.
(() => {
  "use strict";

  // ---- nothing leaves the page ------------------------------------------------------------------------------
  const head = document.head;
  const isStaticLink = (n) => n instanceof HTMLLinkElement && /(^|\/)static\//.test(n.getAttribute("href") || "");
  const headAppend = head.append.bind(head), headAppendChild = head.appendChild.bind(head);
  head.append = (...nodes) => headAppend(...nodes.filter((n) => !isStaticLink(n)));
  head.appendChild = (n) => (isStaticLink(n) ? n : headAppendChild(n));
  class NoSocket extends EventTarget {
    constructor(url) { super(); this.url = String(url); this.readyState = 3; setTimeout(() => this.dispatchEvent(new Event("close")), 0); }
    send() {} close() {}
  }
  NoSocket.CONNECTING = 0; NoSocket.OPEN = 1; NoSocket.CLOSING = 2; NoSocket.CLOSED = 3;
  window.WebSocket = NoSocket;
  window.EventSource = NoSocket;
  if (navigator.sendBeacon) navigator.sendBeacon = () => true;
  try { if (!localStorage.getItem("cc.activeProject")) localStorage.setItem("cc.activeProject", "C:\\Projects\\CommandCenter"); } catch { /* storage blocked */ }

  // ---- time helpers -------------------------------------------------------------------------------------------
  const pad = (n) => String(n).padStart(2, "0");
  const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const hm = (d) => `${ymd(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const T0 = new Date(); T0.setSeconds(0, 0);
  const at = (mins) => hm(new Date(T0.getTime() + mins * 60000));      // minutes from page load
  const ago = (hours) => at(-hours * 60);
  // Today's meetings sit around the current time during the workday; outside it they are pinned to the workday
  // (so an evening visit shows a day that already happened, not meetings at 11 PM).
  const B = (() => { const lo = new Date(T0), hi = new Date(T0); lo.setHours(9, 30, 0, 0); hi.setHours(14, 30, 0, 0); return T0 < lo ? lo : T0 > hi ? hi : new Date(T0); })();
  const atB = (mins) => hm(new Date(B.getTime() + mins * 60000));
  const day = (n) => { const d = new Date(T0); d.setDate(d.getDate() + n); return ymd(d); };
  const dayAt = (n, h, m = 0) => { const d = new Date(T0); d.setDate(d.getDate() + n); d.setHours(h, m, 0, 0); return hm(d); };
  const today = () => ymd(new Date());
  const weekend = (d) => d.getDay() === 0 || d.getDay() === 6;
  let seq = 0x2a10;
  const hid = () => `0000000000D3E0${(seq++).toString(16).toUpperCase().padStart(10, "0")}`;  // Outlook-style hex id

  // ---- people (all invented) ----------------------------------------------------------------------------------
  const P = {
    lena: ["Lena Ortiz", "lena.ortiz@example.com", true], marcus: ["Marcus Lee", "marcus.lee@example.com", true],
    priya: ["Priya Shah", "priya.shah@example.com", true], tom: ["Tom Becker", "tom.becker@example.com", true],
    dana: ["Dana Reyes", "dana.reyes@example.com", true],
    avery: ["Avery Chen", "avery.chen@northwind-audio.example", false], casey: ["Casey Ellis", "casey@fabrikam-av.example", false],
    sam: ["Sam Patel", "sam.patel@contoso-supply.example", false], riley: ["Riley Brooks", "riley@adatum-freight.example", false],
    promo: ["Bulk Deals Daily", "offers@bulkdeals-mailer.example", false],
  };
  const ME = { name: "Jordan Cole", email: "jordan.cole@example.com" };

  // ---- mail ---------------------------------------------------------------------------------------------------
  const BODY = {
    po: "Hi Jordan,\n\nOur receiving team is asking about PO 4410287. The confirmation still shows the original ship date. Can you confirm the revised date, and whether the two back-ordered amplifiers ship with it or separately?\n\nThe install crew is booked for the 4th, so a partial shipment is fine as long as the racks arrive first.\n\nThanks,\nAvery",
    pricing: "Jordan,\n\nI need the Q4 dealer pricing signed off before the 3 PM leadership call. The only open item is the freight surcharge on the West region tier. Can you confirm we're holding it at 4% through December?\n\nLena",
    onboarding: "Hey,\n\nAttached is the updated dealer onboarding checklist. I moved the credit application ahead of the training step like we discussed. Anything else before I send it to the three new dealers?\n\nMarcus",
    freight: "Hello Jordan,\n\nHere is the freight quote for the Harbor Point install: two pallets, liftgate delivery, residential area. Quote is valid for 7 days. Order reference 4410311.\n\nLet me know if you want us to book it.\n\nSam Patel\nContoso Supply",
    vip: "Jordan,\n\nQuick one: the board deck needs the on-time shipping number for September. Do you have it, or should I pull it from the dashboard?\n\nLena",
    newsletter: "LIMITED TIME: 70% off pallet racking, today only! Click to claim your bulk discount before stock runs out.",
    rma: "Hi Jordan,\n\nReturn authorization RA-3390 is approved. The customer will ship the unit back this week. Please flag the credit memo for order 4409876 once the unit is received.\n\nTom",
    lunch: "Team,\n\nLunch and learn next Thursday at noon: the new freight portal. Pizza is on ops. Reply if you have dietary needs.\n\nDana",
    invoice: "Hi,\n\nInvoice 7781 for the October warehouse services is attached and needs your approval in the portal.\n\nRiley Brooks\nAdatum Freight",
    survey: "Jordan,\n\nCould you fill in the dealer satisfaction survey summary by Friday? Two sections left: lead times and support.\n\nPriya",
    shipmap: "FYI: the shipping method map for Northwind is updated in staging. Please review before I push it to production.\n\nMarcus",
  };
  const mk = (p, subject, hours, body, extra = {}) => ({
    id: hid(), subject, sender_name: P[p][0], sender: P[p][1], internal: P[p][2], received: ago(hours), unread: false,
    high_importance: false, flagged: false, to: ME.name, cc: "", folder: "Inbox", body, preview: body.replace(/\s+/g, " ").slice(0, 200), ...extra,
  });
  const M = {
    pricing: mk("lena", "Q4 dealer pricing: sign-off needed today", 0.6, BODY.pricing, { unread: true, high_importance: true }),
    po: mk("avery", "PO 4410287: revised ship date?", 1.4, BODY.po, { unread: true }),
    onboarding: mk("marcus", "Re: Dealer onboarding checklist", 3.2, BODY.onboarding, { cc: "Dana Reyes" }),
    freight: mk("sam", "Freight quote for the Harbor Point install", 5.5, BODY.freight, { unread: true }),
    vip: mk("lena", "September on-time shipping number", 2.1, BODY.vip, { unread: true }),
    newsletter: mk("promo", "Last chance: 70% off pallet racking", 4, BODY.newsletter, { unread: true }),
    rma: mk("tom", "Return authorization RA-3390 approved", 26, BODY.rma, { flagged: true }),
    lunch: mk("dana", "Lunch and learn next Thursday", 28, BODY.lunch),
    invoice: mk("riley", "Invoice 7781 needs your approval", 30, BODY.invoice, { flagged: true }),
    survey: mk("priya", "Dealer survey summary by Friday", 50, BODY.survey, { flagged: true }),
    shipmap: mk("marcus", "Northwind shipping map updated in staging", 6.5, BODY.shipmap),
  };
  // Teams' missed-activity emails
  const teamsMail = (who, subject, preview, hours, urgent = false) => ({
    id: hid(), subject, from_name: who, sender_name: "Microsoft Teams", sender: "noreply@email.teams.microsoft.com",
    received: ago(hours), unread: true, urgent, preview, body: `${who} sent you a message in Microsoft Teams:\n\n"${preview}"\n\nReply in Teams.`,
    to: ME.name, cc: "", folder: "Inbox", high_importance: false, flagged: false,
  });
  const T = {
    priya: teamsMail("Priya Shah", "Priya Shah sent you a message", "Can you look at the Northwind shipping map before 3?", 0.4),
    lena: teamsMail("Lena Ortiz", "Lena Ortiz sent you a message", "Ping me when the pricing sheet is final, leadership call moved up.", 0.2, true),
    marcus: teamsMail("Marcus Lee", "You were mentioned in Sales Ops > General", "@Jordan can you confirm the freight surcharge for West?", 2.5),
  };
  const sentMail = [
    { id: hid(), subject: "Updated quote for Harbor Point", to: "Avery Chen", sent: ago(28), folder: "Sent", received: ago(28) },
    { id: hid(), subject: "Re: Freight claim 5521", to: "Riley Brooks; Sam Patel", sent: ago(52), folder: "Sent", received: ago(52) },
    { id: hid(), subject: "Credit memo request for order 4409876", to: "Tom Becker", sent: ago(75), folder: "Sent", received: ago(75) },
  ];
  const allMail = () => [...Object.values(M), ...Object.values(T), ...sentMail];
  const findMail = (id) => allMail().find((m) => m.id === id);
  const inbox = () => [...Object.values(M), ...Object.values(T)].filter((m) => !spammed.has(m.id)).sort((a, b) => b.received.localeCompare(a.received));

  // ---- state the UI can change ----------------------------------------------------------------------------------
  const dismissed = new Set(), spammed = new Set(), replied = new Set();
  const snoozes = {};   // id -> {until, subject, who, kind}
  let watches = [{ id: sentMail[0].id, subject: sentMail[0].subject, to: "Avery Chen", due: dayAt(2, 9), conversation: "conv-1" }];
  let muted = ["offers@bulkdeals-mailer.example"];
  let urgentPeople = ["Lena Ortiz"];
  // Important senders (people.py): an address or a whole @domain; mail from them gets a star (important: true).
  let importantSenders = ["lena.ortiz@example.com", "@northwind-audio.example"];
  const SHARED_PROVIDERS = ["gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com"];
  const isImportant = (m) => {
    const a = String(m.sender || "").toLowerCase();
    return !!a && importantSenders.some((e) => (e.startsWith("@") ? a.endsWith(e) : a === e));
  };
  const importantState = () => ({ senders: importantSenders.slice(), from_config: [], internal_domains: ["example.com"],
    shared_providers: SHARED_PROVIDERS, max: 200 });
  let focus = { active: false };
  let asanaConnected = true;

  // ---- calendar -----------------------------------------------------------------------------------------------
  const mtg = (subject, start, end, location, organizer, teams = false, extra = {}) => ({
    id: hid(), subject, start, end, location, organizer, all_day: false, status: "busy",
    join_url: teams ? "https://teams.microsoft.com/l/meetup-join/preview-meeting" : null, ...extra,
  });
  let agenda = [
    mtg("Daily ops stand-up", atB(-150), atB(-135), "Microsoft Teams", "Dana Reyes", true),
    mtg("Forecast review with sales leads", atB(-20), atB(25), "Conf Room B", "Marcus Lee"),
    mtg("Vendor call: Contoso Supply", atB(70), atB(100), "Microsoft Teams", "Sam Patel", true),
    mtg("Lunch", atB(105), atB(145), "", ME.name, false, { lunch: true }),
    mtg("Focus: Q4 pricing sheet", atB(150), atB(210), "", ME.name),
    mtg("1:1 with Lena", atB(240), atB(270), "Lena's office", "Lena Ortiz", false, { status: "tentative" }),
  ];
  const futureMeetings = (n) => {
    const d = new Date(T0); d.setDate(d.getDate() + n);
    if (weekend(d)) return [];
    const list = [
      [mtg("Daily ops stand-up", dayAt(n, 8, 30), dayAt(n, 8, 45), "Microsoft Teams", "Dana Reyes", true)],
      [mtg("Dealer onboarding: three new dealers", dayAt(n, 10), dayAt(n, 11), "Microsoft Teams", "Marcus Lee", true)],
      [mtg("Warehouse walkthrough", dayAt(n, 13, 30), dayAt(n, 14, 30), "Warehouse B", "Tom Becker")],
      [mtg("Pricing committee", dayAt(n, 15), dayAt(n, 16), "Conf Room A", "Lena Ortiz")],
    ];
    return [list[0][0], ...(n % 3 === 1 ? list[1] : n % 3 === 2 ? list[2] : list[3])];
  };
  let feeds = [
    { id: "feed-1", name: "Family (Google)", kind: "google", color: "#ff9f43", enabled: true, detail: "titles", count: 9, fetched: at(-12), error: null },
    { id: "feed-2", name: "Kids (iCloud)", kind: "icloud", color: "#a29bfe", enabled: true, detail: "full", count: 4, fetched: at(-12), error: null },
  ];
  const FEED_COLORS = ["#ff9f43", "#a29bfe", "#55efc4", "#fd79a8", "#74b9ff", "#ffeaa7"];
  const overlayEvents = (n) => {
    const out = [];
    const d = new Date(T0); d.setDate(d.getDate() + n);
    const f1 = feeds[0], f2 = feeds[1];
    if (f1?.enabled && n % 2 === 0) out.push({ title: n === 0 ? "Soccer practice" : "Family dinner", start: dayAt(n, 18), end: dayAt(n, 19, 30), all_day: false, calendar: f1.name, color: f1.color, location: n === 0 ? "Riverside fields" : "", busy: f1.detail === "busy" });
    if (f2?.enabled && n === 3) out.push({ title: "School picture day", start: dayAt(n, 0), end: dayAt(n + 1, 0), all_day: true, calendar: f2.name, color: f2.color, location: "", busy: f2.detail === "busy" });
    if (f2?.enabled && weekend(d)) out.push({ title: "Swim lesson", start: dayAt(n, 9, 30), end: dayAt(n, 10, 15), all_day: false, calendar: f2.name, color: f2.color, location: "Community pool", busy: f2.detail === "busy" });
    return out;
  };
  let personalSeq = 1;
  let personal = [
    { id: personalSeq++, day: day(0), title: "Pick up dry cleaning", all_day: true, start: null, end: null, notes: "", updated: ago(20) },
    { id: personalSeq++, day: day(2), title: "Dentist", all_day: false, start: "16:30", end: "17:15", notes: "Bring the insurance card", updated: ago(70) },
    { id: personalSeq++, day: day(5), title: "Renew car registration", all_day: true, start: null, end: null, notes: "", updated: ago(5) },
  ];

  // ---- tasks --------------------------------------------------------------------------------------------------
  let tasks = [
    { source: "outlook", id: hid(), subject: "Load September sell-through numbers", due: day(-2), priority: "high" },
    { source: "asana", id: "1205550001", subject: "Review Q4 promo landing page", due: day(0), priority: "normal", url: "https://app.asana.com/0/0/1205550001", project: "Marketing Ops" },
    { source: "outlook", id: hid(), subject: "Send revised price list to Priya", due: day(0), priority: "high" },
    { source: "asana", id: "1205550002", subject: "Draft install checklist for Harbor Point", due: day(3), priority: "normal", url: "https://app.asana.com/0/0/1205550002", project: "Harbor Point" },
    { source: "outlook", id: hid(), subject: "Approve October freight invoices", due: day(1), priority: "normal" },
    { source: "outlook", id: hid(), subject: "Clean up shared drive folders", due: null, priority: "low" },
  ];

  // ---- today ----------------------------------------------------------------------------------------------------
  const isMuted = (m) => muted.some((s) => [m.sender, m.sender_name].filter(Boolean).some((x) => x.toLowerCase() === s.toLowerCase()));
  const open = (m) => m && !dismissed.has(m.id) && !snoozes[m.id] && !spammed.has(m.id) && !replied.has(m.id) && !isMuted(m);
  const weatherFor = (zip) => {
    const place = zip === "92002" ? { location: "Seacliff", temp: 66, feels: 65, text: "Partly cloudy", icon: "⛅", hi: 69, lo: 57, hum: 64, wind: 11, rain: 10 }
      : { location: "Brightwater", temp: 72, feels: 71, text: "Clear", icon: "☀️", hi: 76, lo: 55, hum: 41, wind: 6, rain: 5 };
    const hours = [1, 2, 3, 4, 5, 6].map((h) => {
      const d = new Date(T0); d.setMinutes(0); d.setHours(d.getHours() + h);
      const night = d.getHours() >= 18 || d.getHours() < 7;
      return { time: hm(d), temp: place.temp + (night ? -h * 2 : 2 - h), icon: night ? "🌙" : h > 4 ? "⛅" : place.icon, text: night ? "Clear" : place.text, rain: h > 4 ? place.rain + 10 : place.rain };
    });
    return { location: place.location, zip, selected: zip, unit: "°F", wind_unit: "mph", temp: place.temp, feels_like: place.feels, humidity: place.hum,
      wind: place.wind, text: place.text, icon: place.icon, high: place.hi, low: place.lo, rain_chance: place.rain,
      sunrise: `${day(0)}T07:04`, sunset: `${day(0)}T18:05`, hours,
      locations: [{ name: "Brightwater", zip: "92001" }, { name: "Seacliff", zip: "92002" }] };
  };
  let weatherZip = "92001";
  const pick = (m, keys) => Object.fromEntries(keys.filter((k) => m[k] !== undefined).map((k) => [k, m[k]]));
  const MAILKEYS = ["id", "subject", "sender_name", "sender", "received", "unread", "internal", "preview", "high_importance", "flagged"];
  const mailOut = (m, keys = MAILKEYS) => ({ ...pick(m, keys), important: isImportant(m) });
  function followups() {
    const urgentNames = new Set(urgentPeople.map((p) => p.toLowerCase()));
    const isUrgent = (m) => urgentNames.has(String(m.sender_name).toLowerCase()) || urgentNames.has(String(m.sender).toLowerCase());
    const reply = [M.pricing, M.po, M.onboarding, M.freight].filter(open);
    const flagged = [M.rma, M.invoice, M.survey].filter((m) => m.flagged && open(m));
    return {
      urgent_reply: reply.filter(isUrgent).map((m) => mailOut(m)),
      needs_reply: reply.filter((m) => !isUrgent(m)).map((m) => mailOut(m)),
      teams_reply: Object.values(T).filter((m) => m.unread && open(m)).map((m) => ({ ...mailOut(m), from_name: m.from_name, urgent: m.urgent || urgentNames.has(m.from_name.toLowerCase()) })),
      awaiting_reply: sentMail.filter(open).map((m) => pick(m, ["id", "subject", "to", "sent"])),
      flagged: flagged.map((m, i) => ({ ...mailOut(m), due: [day(-1), day(0), null][i] })),
      unread_vip: Object.values(M).filter((m) => m.unread && isImportant(m) && open(m)).map((m) => mailOut(m)),
      flagged_hidden: { automated: 37, older: 112, more: 0, before: 1284, since: day(-90), total: 1436 },
    };
  }
  const openTasks = () => tasks.filter((t) => !snoozes[t.id]);
  const todayData = () => ({
    now: at(0), weather: weatherFor(weatherZip), agenda: agenda.slice().sort((a, b) => a.start.localeCompare(b.start)),
    tasks: { items: openTasks(), asana: { connected: asanaConnected } },
    followups: followups(), snoozed: Object.keys(snoozes).length, reply_watches: watches.length,
  });

  // ---- alerts ---------------------------------------------------------------------------------------------------
  let alertSeq = 1;
  const alert = (source, title, body, priority, action, hours, read = false) => ({ id: alertSeq++, ts: ago(hours), source, title, body, priority, action, read: read ? 1 : 0 });
  let alerts = [
    alert("Calendar", `Coming up at ${new Date(agenda[2].start).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}: Vendor call: Contoso Supply`, "Microsoft Teams", "high", { type: "meeting", id: agenda[2].id, join_url: agenda[2].join_url, prep: true }, 0.05),
    alert("Urgent", "Urgent Teams message from Lena Ortiz", "Ping me when the pricing sheet is final, leadership call moved up.", "high", { type: "teams", url: "https://teams.microsoft.com/l/chat/0/0?users=lena.ortiz@example.com" }, 0.2),
    alert("Outlook", "VIP: Q4 dealer pricing: sign-off needed today", "From Lena Ortiz", "high", { type: "mail", id: M.pricing.id }, 0.6),
    alert("Pulse", "Pulse #1047 assigned to you", "Dealer locator shows closed stores", "normal", { type: "pulse", url: "https://pulse.example.com/feedback?ticket=1047", label: "Open in Pulse" }, 1.1),
    alert("Order", "Order 4410287 changed: Released → Shipped", "Northwind Audio · 2 lines", "normal", { type: "order", number: "4410287" }, 1.6),
    alert("Website", "Carrier tracking PRO 77812: value changed", "In transit → Out for delivery", "normal", { type: "url", url: "https://tracking.example.com/pro/77812" }, 2.4),
    alert("Calendar", "Forecast review ended: capture notes?", "Notes and action items, encrypted on this PC", "normal", { type: "meeting-notes", id: agenda[1].id, start: agenda[1].start, end: agenda[1].end }, 2.9, true),
    alert("System", "2 projects have uncommitted work", "CommandCenter · dealer-portal", "normal", { type: "uncommitted" }, 5, true),
    alert("Today", "72°F clear · 5 meetings · 3 tasks due · 11 follow-ups", "First up: Forecast review at the top of the hour", "normal", { type: "today" }, 7, true),
    alert("Follow-up", "No reply yet: Updated quote for Harbor Point", "Sent to Avery Chen 2 days ago", "normal", { type: "mail", id: sentMail[0].id }, 20, true),
  ];
  const unreadAlerts = () => alerts.filter((a) => !a.read).length;

  // ---- Teams ------------------------------------------------------------------------------------------------------
  const teamsState = () => ({ ok: true, unread: 5, badge: 4, dms_unread: 2, groups_unread: 1, meetings_unread: 0, mentions_unread: 1, checked: at(-2) });
  const teamsRecent = () => [
    { kind: "dm", who: "Lena Ortiz", preview: "Ping me when the pricing sheet is final, leadership call moved up.", time: ago(0.2), unread: true, urgent: true, count: 2, link: "https://teams.microsoft.com/l/chat/0/0?users=lena.ortiz@example.com" },
    { kind: "dm", who: "Priya Shah", preview: "Can you look at the Northwind shipping map before 3?", time: ago(0.4), unread: true, count: 1, link: "https://teams.microsoft.com/l/chat/0/0?users=priya.shah@example.com" },
    { kind: "mention", who: "Marcus Lee", where: "Sales Ops > General", preview: "@Jordan can you confirm the freight surcharge for West?", time: ago(2.5), unread: true, link: "https://teams.microsoft.com/l/message/0/0" },
    { kind: "group", who: "Harbor Point install crew", where: "Harbor Point install", preview: "Racks are on site, waiting on the amplifiers.", time: ago(4), unread: true, count: 3, link: null },
    { kind: "meeting", who: "Daily ops stand-up", where: "Meeting chat", preview: "Dana: notes are in the channel.", time: ago(6), unread: false, link: null },
  ];

  // ---- Pulse ------------------------------------------------------------------------------------------------------
  const tk = (no, title, detail, type, status, priority, requester, mine, createdH, waitingDays, extra = {}) => ({
    id: `t-${no}`, ticket_no: no, title, detail, type, where: extra.where || "Dealer portal / Orders", requester, with: status === "Resolved" ? null : extra.with || "Pulse team",
    status, priority, mine, created_at: ago(createdH), updated_at: ago(Math.min(createdH, 3)), resolved_by: status === "Resolved" ? ME.name : null,
    url: `https://pulse.example.com/feedback/${no}`, waiting_on_me: waitingDays != null && mine && status === "Open",
    waiting_since: waitingDays != null ? ago(waitingDays * 24) : null, age_days: waitingDays,
    aging: waitingDays == null ? null : waitingDays >= 5 ? "overdue" : waitingDays >= 2 ? "warn" : "fresh",
  });
  const tickets = [
    tk(1047, "Dealer locator shows closed stores", "Two stores that closed in August still show up in the locator for ZIP searches.", "DataIssue", "Open", "high", "Avery Chen", true, 26, 1, { where: "Website / Dealer locator" }),
    tk(1041, "Order history export is missing freight", "The CSV export leaves out the freight line on multi-ship orders.", "Bug", "Open", "normal", "Marcus Lee", true, 96, 3),
    tk(1033, "Add ship-to phone to the order view", "Customer service asks for the ship-to phone number on the order detail page.", "FeatureRequest", "Open", "low", "Priya Shah", true, 170, 6),
    tk(1052, "Price list PDF shows last year's logo", "", "Feedback", "Open", "normal", "Tom Becker", true, 3, 0, { where: "Dealer portal / Price lists" }),
    tk(1029, "Back-order dates shift by one day", "Back-order ETA shows a day early for West warehouse items.", "Bug", "Waiting on reporter", "normal", "Casey Ellis", true, 240, null, { with: "reporter" }),
    tk(1038, "Review sell-through numbers for Q3", "Data review before the quarterly report.", "DataReview", "On hold", "normal", "Lena Ortiz", true, 300, null),
    tk(1044, "Login page loops on old bookmark", "", "Bug", "Open", "high", "Dana Reyes", false, 50, null, { where: "Dealer portal / Sign in" }),
    tk(1019, "Duplicate dealer accounts after merge", "", "DataIssue", "Resolved", "normal", "Sam Patel", true, 500, null),
  ];
  function pulseSummary() {
    const open = tickets.filter((t) => t.status !== "Resolved");
    const waiting = tickets.filter((t) => t.waiting_on_me).sort((a, b) => (b.age_days ?? -1) - (a.age_days ?? -1));
    const c = (b) => waiting.filter((t) => t.aging === b).length;
    return { configured: true, open_mine: open.filter((t) => t.mine && t.status !== "On hold").length,
      new_today: tickets.filter((t) => t.created_at.slice(0, 10) === today()).length,
      high_priority: open.filter((t) => t.priority === "high").length, open_total: open.length,
      on_hold: open.filter((t) => t.status === "On hold").length,
      aging_fresh: c("fresh"), aging_warn: c("warn"), aging_overdue: c("overdue"), aging_unknown: 0, aging_waiting: waiting.length,
      oldest_days: waiting[0]?.age_days ?? null, oldest_ticket_no: waiting[0]?.ticket_no ?? null, aging_warn_days: 2, aging_overdue_days: 5, as_of: at(-1) };
  }
  const pulseStatus = () => ({ configured: true, enabled: true, driver: true, host: "db.pulse.example", port: 5432, database: "postgres",
    user: "commandcenter_ro.demo", ssl_root_cert: "system", url: "https://pulse.example.com", poll_minutes: 5, last_poll: at(-1), last_ok: at(-1),
    ok: true, error: null, setup_sql: "-- The setup SQL is shown in the desktop app.\n-- It creates a read-only login that can read only the ticket columns Command Center shows." });

  // ---- SQL ----------------------------------------------------------------------------------------------------
  const sql = {
    connections: [
      { name: "orders", server: "sql-demo01", database: "OrdersDemo", username: "cc_readonly", auth: null, password_saved: true },
      { name: "warehouse", server: "sql-demo02", database: "WarehouseDemo", username: null, auth: "windows", password_saved: false },
    ],
    queries: [
      { name: "Find order", connection: "orders", params: ["Order number"], description: "Order header and status by order number" },
      { name: "Open orders by customer", connection: "orders", params: ["Customer ID"], description: "Everything not yet shipped for one customer" },
      { name: "Late shipments this week", connection: "warehouse", params: [], description: "Shipments past their promised date" },
    ],
  };
  const orderRows = {
    columns: ["OrderNo", "Customer", "Status", "Lines", "ShipDate", "Total"],
    rows: [["4410287", "Northwind Audio", "Shipped", "6", day(-1), "18,450.00"], ["4410311", "Harbor Point Residences", "Released", "2", day(2), "3,120.50"],
      ["4409876", "Fabrikam AV", "Credit hold", "1", null, "960.00"], ["4410150", "Northwind Audio", "Picking", "4", day(1), "7,215.75"]],
    truncated: false, seconds: 0.21,
  };
  const TABLES = ["dbo.Customers", "dbo.OrderLines", "dbo.Orders", "dbo.Shipments", "dbo.ShipMethods", "dbo.Warehouses", "sales.PriceLists", "sales.Promotions"];

  // ---- Toolkit ------------------------------------------------------------------------------------------------
  const TK_TABLES = {
    ship_account: { label: "Shipping accounts", name: "[STAGINGDB].[sps].[SHIPPING_ACCOUNT]", pk: ["ACCOUNT_ID"], columns: ["ACCOUNT_ID", "ACCOUNT_NAME", "PARTNER_ID", "ACCOUNT_NUMBER"], constants: {}, unique: ["PARTNER_ID", "ACCOUNT_NUMBER"] },
    ship_method: { label: "Shipping methods", name: "[STAGINGDB].[sps].[SHIPPING_METHOD]", pk: ["SHIPPING_METHOD_ID"], columns: ["SHIPPING_METHOD_ID", "ACCOUNT_ID", "INBOUND_ID", "CARRIER_ID", "SHIP_VIA", "FREIGHT_TERMS"], constants: {}, unique: ["ACCOUNT_ID", "INBOUND_ID"] },
    logi_users: { label: "Logi users", name: "[LogiDemo].[dbo].[Users]", pk: ["UserID"], columns: ["UserID", "Password", "EmailID", "FullName"], constants: {} },
    logi_roles: { label: "Logi roles", name: "[LogiDemo].[dbo].[UserRoles]", pk: ["UserID", "RoleID"], columns: ["UserID", "RoleID"], constants: { RoleID: "Dealer" } },
    logi_rights: { label: "Logi rights", name: "[LogiDemo].[dbo].[UserRights]", pk: ["UserID", "RightID"], columns: ["UserID", "RightID"], constants: { RightID: "Dashboard" } },
    logi_dash: { label: "Customer tree access", name: "[SYSDB].[dbo].[TREE_ACCESS]", pk: ["OPRID", "TREE_NODE"], columns: ["OPRID", "TREE_NAME", "TREE_NODE", "DEFAULT_TREE_NAME"], constants: {} },
  };
  const tkMeta = () => ({
    configured: true, password_saved: true,
    last_test: { login: "cc_toolkit", server: "sql-demo01", database: "STAGINGDB", at: ago(3) },
    settings: { server: "sql-demo01", username: "cc_toolkit", driver: "ODBC Driver 18 for SQL Server", encrypt: true, trust_server_certificate: false,
      databases: { staging: "STAGINGDB", logi: "LogiDemo", sys: "SYSDB" }, shipping_schema: "sps",
      tree: { user: "DEMO", tree: "CUST_ROLLUP", setid: "SHARE", as_of: "2026-01-01" } },
    drivers: ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"], installed_drivers: ["ODBC Driver 18 for SQL Server"],
    cert_pin: { sha256: "4f1a9c2be07d3388a1c5e6f2b9d04a7731c8e2f05b6d9a1e3c7f20b48d5e6a19", subject: "CN=sql-demo01", valid_to: "2027-03-01", pinned_at: ago(400) },
    keyring: { service: "CommandCenter/toolkit", user: "cc_toolkit@sql-demo01" }, tables: TK_TABLES,
  });
  const shipAccounts = [
    { ACCOUNT_ID: 101, ACCOUNT_NAME: "Northwind Audio", PARTNER_ID: "NWAUDIO", ACCOUNT_NUMBER: "C-20418" },
    { ACCOUNT_ID: 102, ACCOUNT_NAME: "Fabrikam AV", PARTNER_ID: "FABRIKAM", ACCOUNT_NUMBER: "C-20577" },
    { ACCOUNT_ID: 103, ACCOUNT_NAME: "Harbor Point Residences", PARTNER_ID: "HARBORPT", ACCOUNT_NUMBER: "C-21003" },
    { ACCOUNT_ID: 104, ACCOUNT_NAME: "Contoso Home Theater", PARTNER_ID: "CONTOSOHT", ACCOUNT_NUMBER: null },
  ];
  const shipMethods = {
    101: [{ SHIPPING_METHOD_ID: 311, ACCOUNT_ID: 101, INBOUND_ID: "FDEG", CARRIER_ID: "FEDEX", SHIP_VIA: "FEDEX GROUND", FREIGHT_TERMS: "PREPAID" },
      { SHIPPING_METHOD_ID: 312, ACCOUNT_ID: 101, INBOUND_ID: "UPSN", CARRIER_ID: "UPS", SHIP_VIA: "UPS NEXT DAY", FREIGHT_TERMS: "COLLECT" }],
    102: [{ SHIPPING_METHOD_ID: 320, ACCOUNT_ID: 102, INBOUND_ID: "ODFL", CARRIER_ID: "ODFL", SHIP_VIA: "OLD DOMINION LTL", FREIGHT_TERMS: "PREPAID" }],
    103: [], 104: [{ SHIPPING_METHOD_ID: 340, ACCOUNT_ID: 104, INBOUND_ID: "FDEG", CARRIER_ID: "FEDEX", SHIP_VIA: "FEDEX GROUND", FREIGHT_TERMS: "THIRD PARTY" }],
  };
  const logiUsers = [
    { UserID: "NWAUDIO01", EmailID: "portal@northwind-audio.example", FullName: "Northwind Audio (portal)", has_password: true },
    { UserID: "FABRIKAM02", EmailID: "orders@fabrikam-av.example", FullName: "Fabrikam AV Orders", has_password: true },
    { UserID: "CONTOSO03", EmailID: null, FullName: "Contoso Home Theater", has_password: false },
  ];
  const logiDash = [
    { OPRID: "NWAUDIO01", TREE_NAME: "CUST_ROLLUP", TREE_NODE: "WEST-NW", DEFAULT_TREE_NAME: "CUST_ROLLUP" },
    { OPRID: "FABRIKAM02", TREE_NAME: "CUST_ROLLUP", TREE_NODE: "EAST-FAB", DEFAULT_TREE_NAME: null },
    { OPRID: "FABRIKAM02", TREE_NAME: "CUST_ROLLUP", TREE_NODE: "EAST-FAB-2", DEFAULT_TREE_NAME: null },
    { OPRID: "OLDUSER09", TREE_NAME: "CUST_ROLLUP", TREE_NODE: "WEST-OLD", DEFAULT_TREE_NAME: null },
  ];
  const tree = { tree: "CUST_ROLLUP", as_of: "2026-01-01", rows: [
    { node: "ALL", label: "All customers", level: 1 }, { node: "WEST", label: "West region", level: 2 },
    { node: "WEST-NW", label: "Northwind Audio", level: 3 }, { node: "WEST-HP", label: "Harbor Point Residences", level: 3 },
    { node: "WEST-OLD", label: "Closed West accounts", level: 3 }, { node: "EAST", label: "East region", level: 2 },
    { node: "EAST-FAB", label: "Fabrikam AV", level: 3 }, { node: "EAST-FAB-2", label: "Fabrikam AV (second warehouse)", level: 4 },
    { node: "EAST-CHT", label: "Contoso Home Theater", level: 3 } ] };
  const tkChanges = [
    { id: 58, ts: ago(3).replace("T", "T") + ":12", ok: 1, action: "update", table_key: "ship_method", table_name: TK_TABLES.ship_method.name, pk: { SHIPPING_METHOD_ID: 312 },
      before_json: { SHIP_VIA: "UPS 2ND DAY", FREIGHT_TERMS: "COLLECT" }, after_json: { SHIP_VIA: "UPS NEXT DAY", FREIGHT_TERMS: "COLLECT" }, windows_user: "DEMO\\jcole", connection: "cc_toolkit@sql-demo01", error: null, batch: null },
    { id: 57, ts: ago(27) + ":40", ok: 1, action: "insert", table_key: "logi_dash", table_name: TK_TABLES.logi_dash.name, pk: null,
      before_json: {}, after_json: { OPRID: "FABRIKAM02", TREE_NAME: "CUST_ROLLUP", TREE_NODE: "EAST-FAB-2" }, windows_user: "DEMO\\jcole", connection: "cc_toolkit@sql-demo01", error: null, batch: "b-19" },
    { id: 56, ts: ago(28) + ":05", ok: 0, action: "delete", table_key: "ship_account", table_name: TK_TABLES.ship_account.name, pk: { ACCOUNT_ID: 99 },
      before_json: { ACCOUNT_ID: 99, ACCOUNT_NAME: "Test account" }, after_json: {}, windows_user: "DEMO\\jcole", connection: "cc_toolkit@sql-demo01", error: "The row changed since it was loaded; nothing was deleted.", batch: null },
    { id: 55, ts: ago(50) + ":31", ok: 1, action: "reveal", table_key: "logi_users", table_name: TK_TABLES.logi_users.name, pk: { UserID: "NWAUDIO01" },
      before_json: {}, after_json: {}, windows_user: "DEMO\\jcole", connection: "cc_toolkit@sql-demo01", error: null, batch: null },
  ];
  const userTables = (uid) => {
    const u = uid.toUpperCase();
    const rowsOf = (list, key) => list.filter((r) => String(r[key]).toUpperCase() === u);
    return {
      logi_users: { label: "Users", columns: ["UserID", "Password", "EmailID", "FullName"], rows: rowsOf(logiUsers, "UserID").map((r) => ({ ...r, Password: r.has_password ? "(hidden)" : null })) },
      logi_roles: { label: "Roles", columns: ["UserID", "RoleID"], rows: rowsOf(logiUsers, "UserID").map((r) => ({ UserID: r.UserID, RoleID: "Dealer" })) },
      logi_rights: { label: "Rights", columns: ["UserID", "RightID"], rows: rowsOf(logiUsers, "UserID").map((r) => ({ UserID: r.UserID, RightID: "Dashboard" })) },
      logi_dash: { label: "Customer tree access", columns: ["OPRID", "TREE_NAME", "TREE_NODE", "DEFAULT_TREE_NAME"], rows: rowsOf(logiDash, "OPRID") },
    };
  };

  // ---- projects, VS Code, daily ------------------------------------------------------------------------------
  const projects = [
    { name: "CommandCenter", path: "C:\\Projects\\CommandCenter", branch: "main", writable_by_others: false },
    { name: "dealer-portal", path: "C:\\Projects\\dealer-portal", branch: "feature/price-lists", writable_by_others: false },
    { name: "edi-mapper", path: "C:\\Projects\\edi-mapper", branch: "main", writable_by_others: false },
    { name: "sandbox", path: "C:\\Users\\Public\\sandbox", branch: "main", writable_by_others: true },
  ];
  const uncommitted = () => ({ show: true, checked: at(0), projects: [
    { name: "CommandCenter", path: projects[0].path, branch: "main", changed_files: 7, ahead: 0 },
    { name: "dealer-portal", path: projects[1].path, branch: "feature/price-lists", changed_files: 0, ahead: 2 } ] });
  function recap(d) {
    const dd = d || (() => { const x = new Date(T0); x.setDate(x.getDate() - 1); while (weekend(x)) x.setDate(x.getDate() - 1); return ymd(x); })();
    const t = (h, m) => `${dd}T${pad(h)}:${pad(m)}`;
    return { day: dd, errors: {},
      commits: [
        { project: "CommandCenter", commits: [{ sha: "a41c9e2", subject: "Off-hours tile: week total and live state", at: t(16, 42) },
          { sha: "7d02b18", subject: "Triage: review recipients before sending", at: t(14, 5) }, { sha: "c3f5a90", subject: "Fix agenda free-slot rounding", at: t(10, 18) }] },
        { project: "dealer-portal", commits: [{ sha: "9be1774", subject: "Price list PDF: new logo and footer", at: t(11, 30) }] } ],
      tasks: [{ subject: "Send freight claim paperwork", source: "outlook" }, { subject: "Update install schedule", source: "asana" }],
      pulse: [{ ticket_no: 1019, title: "Duplicate dealer accounts after merge" }],
      meetings: [{ id: hid(), subject: "Daily ops stand-up", start: t(8, 30), has_notes: false }, { id: hid(), subject: "Pricing committee", start: t(15, 0), has_notes: true }],
      sent: { count: 14, items: [{ subject: "Re: Harbor Point schedule", to: "Avery Chen", at: t(9, 12) }, { subject: "Freight claim 5521 paperwork", to: "Riley Brooks", at: t(11, 47) }, { subject: "Re: West region surcharge", to: "Lena Ortiz", at: t(16, 3) }] },
      followups_cleared: 6 };
  }
  const notes = new Map();  // `${id}|${start}` -> note
  notes.set(`${agenda[0].id}|${agenda[0].start}`, { id: agenda[0].id, start: agenda[0].start, end: agenda[0].end, subject: agenda[0].subject,
    notes: "Warehouse B short two pickers this week.\nFreight portal training moved to Thursday.", updated: ago(2),
    actions: [{ text: "Ask Tom for the overtime plan", done: false, task: null }, { text: "Send training invite", done: true, task: { source: "outlook", id: hid() } }] });
  const noteKeys = () => [...notes.values()].map((n) => ({ id: n.id, start: n.start }));

  // ---- reminders / vault / privacy ------------------------------------------------------------------------------
  let vault = { set_up: true, unlocked: true, privacy: false, available: true, retry_after: 0, legacy: {}, min_length: 12 };
  const vaultStatus = () => ({ ...vault, available: vault.unlocked && !vault.privacy });
  let remSeq = 10;
  let reminders = [
    { id: 1, text: "Stretch and refill water", note: "", time: "10:30", repeat: "weekdays", days: [], date: null, enabled: true, snooze: true },
    { id: 2, text: "Take the 3 PM walk", note: "Around the building, 15 minutes", time: "15:00", repeat: "daily", days: [], date: null, enabled: true, snooze: true },
    { id: 3, text: "Call home", note: "", time: "17:30", repeat: "days", days: ["mon", "wed", "fri"], date: null, enabled: true, snooze: false },
    { id: 4, text: "Order a birthday gift for Sam", note: "", time: "12:00", repeat: "once", date: day(3), days: [], enabled: false, snooze: true },
  ];
  const doneToday = new Set([1]);
  const REPEAT_LABEL = { weekdays: "Weekdays", daily: "Every day", once: "Once" };
  const DOW = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
  const repeatLabel = (r) => r.repeat === "days" ? r.days.map((d) => d[0].toUpperCase() + d.slice(1)).join(", ") : REPEAT_LABEL[r.repeat] || r.repeat;
  const runsOn = (r, d) => r.enabled && (r.repeat === "daily" || (r.repeat === "weekdays" && !weekend(d)) || (r.repeat === "days" && r.days.includes(DOW[d.getDay()])) || (r.repeat === "once" && r.date === ymd(d)));
  const nextOf = (r) => {
    for (let i = 0; i < 14; i++) {
      const d = new Date(); d.setDate(d.getDate() + i);
      if (!runsOn(r, d)) continue;
      const [h, m] = r.time.split(":").map(Number);
      d.setHours(h, m, 0, 0);
      if (d > new Date() && !(i === 0 && doneToday.has(r.id))) return hm(d);
    }
    return null;
  };
  function remindersData() {
    const now = new Date(), cur = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
    const todays = reminders.filter((r) => runsOn(r, now)).sort((a, b) => a.time.localeCompare(b.time)).map((r) => ({
      ...r, done: doneToday.has(r.id), open: r.time <= cur, overdue: r.time <= cur && !doneToday.has(r.id), wake: null,
      repeat_label: repeatLabel(r), next: nextOf(r) }));
    const in7 = personal.filter((p) => p.day >= day(0) && p.day <= day(7)).sort((a, b) => a.day.localeCompare(b.day));
    return { state: vaultStatus(), counts: { today: todays.length, done: todays.filter((t) => t.done).length }, snooze_minutes: 15,
      today: vaultStatus().available ? todays : [], reminders: vaultStatus().available ? reminders.map((r) => ({ ...r, repeat_label: repeatLabel(r), next: nextOf(r) })) : [],
      upcoming: vaultStatus().available ? in7 : [], recent: vaultStatus().available ? personal.slice().sort((a, b) => b.updated.localeCompare(a.updated)) : [] };
  }
  let demoPop = [];  // a reminder pop-up, only when asked for from the palette
  function inventory() {
    const v = vaultStatus(), dp = "Windows DPAPI (this Windows account)";
    const item = (id, name, category, accesses, stored, encryption, retention, connected, state, actions = [], extra = {}) =>
      ({ id, name, category, accesses, stored, encryption, retention, connected, state, actions, ...extra });
    return [
      item("outlook", "Outlook", "Work", "Mail, calendar and To Do in the Outlook desktop app on this PC (through Outlook itself; Command Center has no cloud access).",
        "Follow-up state (snoozes, reply checks, dismissed and muted items, urgent people), the sent-mail log, alerts.", dp, "Sent-mail log 90 days; dismissed follow-ups 60 days", true, "Connected"),
      item("teams", "Teams", "Work", "Teams on the web, read-only by a background Edge profile you signed in to: unread counts, unread chats and @mentions.",
        "That browser profile in the private data folder; the unread counts; the chat ids already alerted on.", "Edge's own encryption (DPAPI); alerted ids: " + dp, "Profile until you sign out of it", true, "Signed in"),
      item("toolkit", "Toolkit (SQL Server)", "Work", "Shipping, Logi and Customers data in SQL Server, with your SQL login.",
        "SQL password in Windows Credential Manager; every change attempt in the toolkit change log.", "Credential Manager (DPAPI); change log: " + dp, "Change log 365 days", true, "Password saved", ["forget"]),
      item("pulse", "Pulse tickets", "Work", "Pulse data reviews in Postgres through a read-only role.", "Database password in Windows Credential Manager; the last ticket state.",
        "Credential Manager (DPAPI); ticket state: " + dp, "Until disconnected", true, "Connected", ["disconnect"]),
      item("asana", "Asana", "Work", "Your Asana tasks, read with your personal access token.", "The token in Windows Credential Manager. Tasks are read live, not stored.",
        "Credential Manager (DPAPI)", "Token until disconnected", asanaConnected, asanaConnected ? "Connected" : "Not connected", asanaConnected ? ["disconnect"] : []),
      item("daily", "Meeting notes", "Work", "What you type into meeting notes and their action items.", `${notes.size} meeting note(s) in SQLite.`, dp, "Until deleted", null, `${notes.size} note(s) · visible in Privacy mode (work data)`),
      item("offhours", "Off-hours work log", "Work", "Only whether this PC had keyboard or mouse input while Windows was unlocked, never which app, window, file or key.",
        "Start and end times of activity outside work hours, per day.", dp, "180 days", true, "On · 41 day(s) logged · visible in Privacy mode (work data)"),
      item("weather", "Weather", "Work", "Only zip codes are sent (to the weather services). No account.", "The zip codes' place names and the latest forecast.", "None (zip codes only)", "Forecast: 10 minutes", true, "2 location(s)"),
      item("personal-entries", "Personal calendar entries", "Personal", "Only what you type into personal entries.", "Each entry as one encrypted record in SQLite.",
        "Personal vault: AES-256-GCM, key from your passphrase (not stored anywhere)", "Until you delete them", v.available, `${personal.length} entr(ies) · ${v.available ? "Unlocked" : vault.privacy ? "Privacy mode on" : "Locked"}`),
      item("overlay-calendars", "Overlay calendars (Google / iCloud)", "Personal", "Your private iCal links to Google Calendar / iCloud calendars (read-only).",
        "Each calendar's name, color, detail level and private link as one encrypted record. Events: memory only, 15 minutes.",
        "Personal vault: AES-256-GCM, key from your passphrase (not stored anywhere)", "Links until removed; events 15 min in memory", v.available && feeds.length > 0,
        v.available ? `${feeds.length} calendar(s) · Unlocked` : "Locked", [], { calendars: v.available ? feeds.map((f) => pick(f, ["id", "name", "kind", "detail", "enabled", "color"])) : [] }),
      item("reminders", "Personal reminders", "Personal", "Only the reminders you add.", "Text and notes in the personal vault; a times-only schedule copy (DPAPI).",
        "Personal vault (text); DPAPI (times)", "Until you delete them", v.available, `${reminders.length} reminder(s)`),
    ];
  }

  // ---- off-hours -------------------------------------------------------------------------------------------------
  function offhours() {
    const now = new Date(), monday = new Date(now); monday.setDate(now.getDate() - ((now.getDay() + 6) % 7));
    const SESS = [[["06:40", "07:25"]], [], [["19:10", "20:05"], ["21:30", "21:50"]], [["06:55", "07:40"]], [], [["09:40", "10:25"]], [["20:00", "20:35"]]];
    const mins = (a, b) => { const [h1, m1] = a.split(":").map(Number), [h2, m2] = b.split(":").map(Number); return h2 * 60 + m2 - h1 * 60 - m1; };
    const days = Array.from({ length: 7 }, (_, i) => {
      const d = new Date(monday); d.setDate(monday.getDate() + i);
      const future = ymd(d) > ymd(now);
      const sessions = future ? [] : SESS[i].map(([s, e]) => ({ start: s, end: e, minutes: mins(s, e) }));
      return { day: ymd(d), workday: i < 5, future, minutes: sessions.reduce((n, s) => n + s.minutes, 0), sessions };
    });
    const todayRow = days.find((d) => d.day === ymd(now));
    return { enabled: true, today: todayRow, week_minutes: days.reduce((n, d) => n + d.minutes, 0), days, active: false, tracking_since: ago(24 * 41), last_sample: at(0),
      settings: { work_start: "08:00", work_end: "17:00", work_days: ["mon", "tue", "wed", "thu", "fri"], idle_minutes: 5 } };
  }

  // ---- spam ------------------------------------------------------------------------------------------------------
  let blocks = [{ id: "blk-1", kind: "domain", value: "promo-blast.example", label: "@promo-blast.example", created: ago(24 * 9) },
    { id: "blk-2", kind: "subject", value: "you have won", label: "subject “you have won”", created: ago(24 * 20) }];
  let blockSeq = 3;
  const previews = new Map();

  // ---- profile ---------------------------------------------------------------------------------------------------
  const PRESETS = {
    neon: { label: "Neon Green", accent: "#39ff14", accent2: "#00e5ff" }, cyan: { label: "Cyber Cyan", accent: "#00e5ff", accent2: "#39ff14" },
    synth: { label: "Synthwave Magenta", accent: "#ff2bd6", accent2: "#00e5ff" }, amber: { label: "Amber Terminal", accent: "#ffb000", accent2: "#ff6a3d" },
    ice: { label: "Ice Blue", accent: "#7cc4ff", accent2: "#b8f2ff" }, violet: { label: "Ultraviolet", accent: "#b98cff", accent2: "#00e5ff" },
  };
  const PROFILE_DEFAULTS = { name: ME.name, initials: "JC", preset: "neon", accent: "#39ff14", accent2: "#00e5ff", text_scale: 100, density: "comfortable",
    glow: true, glass: true, aurora: true, grid: true, animations: true, splash: false };
  let profile = { ...PROFILE_DEFAULTS };
  function saveProfile(p) {
    const next = { ...profile };
    for (const [k, v] of Object.entries(p || {})) if (k in PROFILE_DEFAULTS) next[k] = v;
    if (!/^#[0-9a-f]{6}$/i.test(next.accent) || !/^#[0-9a-f]{6}$/i.test(next.accent2)) throw new Error("Colors must look like #39ff14");
    next.initials = String(next.initials || "").trim().toUpperCase();
    if (!/^[A-Z0-9]{1,3}$/.test(next.initials)) throw new Error("Initials: 1-3 letters or digits");
    if (!String(next.name || "").trim()) throw new Error("Name: letters, spaces, . ' - only (up to 60)");
    if (PRESETS[next.preset]) { next.accent = PRESETS[next.preset].accent; next.accent2 = PRESETS[next.preset].accent2; }
    profile = next;
    return profile;
  }

  // ---- websites ----------------------------------------------------------------------------------------------------
  const monitors = () => [
    { name: "Carrier tracking: PRO 77812", url: "https://tracking.example.com/pro/77812", profile: "public", alert_on: "change", interval_minutes: 15, enabled: true,
      state: { ok: true, value: "Out for delivery", checked: at(-6), changed: ago(2.4) } },
    { name: "Dealer portal status page", url: "https://status.dealer-portal.example", profile: "public", alert_on: "change", interval_minutes: 5, enabled: true,
      state: { ok: true, value: "All systems operational", checked: at(-2), changed: ago(30) } },
    { name: "Supplier portal: open POs", url: "https://suppliers.contoso-supply.example/pos", profile: "supplier", alert_on: "above", threshold: 20, interval_minutes: 30, enabled: true,
      state: { ok: false, login_needed: true, error: "The session expired: log in again to keep checking this page.", checked: at(-25) } },
    { name: "Freight rate index", url: "https://rates.example.com/ltl", profile: "public", alert_on: "change", interval_minutes: 60, enabled: false, state: {} },
  ];

  // ---- responses ---------------------------------------------------------------------------------------------------
  const json = (data, status = 200, wait = 40) => new Promise((resolve) => setTimeout(() => resolve(new Response(JSON.stringify(data ?? {}), {
    status, headers: { "Content-Type": "application/json" } })), wait));
  const fail = (status, error) => json({ error }, status);
  const DESKTOP_ONLY = "That works only in the Command Center desktop app (this is a preview with sample data).";
  const hostOf = (u) => { try { return new URL(u).host || u; } catch { return String(u); } };

  function toast(text) {
    let box = document.querySelector("#toast");
    if (!box) { box = document.createElement("div"); box.id = "toast"; box.setAttribute("role", "status"); box.setAttribute("aria-live", "polite"); document.body.append(box); }
    box.textContent = text;
    box.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => box.classList.remove("show"), 3500);
  }
  window.__ccPreview = { unknown: [] };

  function get(path, q) {
    switch (path) {
      case "/status": return json({ outlook: { ok: true, checked: at(0) }, websites: Object.fromEntries(monitors().filter((m) => m.enabled).map((m) => [m.name, m.state])),
        teams: teamsState(), focus, ui: { accent: profile.accent, workday: ["08:00", "17:00"] }, desktop: false, config_path: "config.yaml" });
      case "/alerts": return json({ alerts: (q.get("unread_only") === "true" ? alerts.filter((a) => !a.read) : alerts).slice().sort((a, b) => b.ts.localeCompare(a.ts)), unread: unreadAlerts() });
      case "/today": return json(todayData(), 200, 120);
      case "/mail": {
        const term = (q.get("q") || "").toLowerCase();
        const all = inbox();
        if (!term && q.get("folder") === "sent") {
          return json({ mail: sentMail.map((m) => ({ ...m, sender_name: ME.name, sender: ME.email, unread: false, important: false, preview: m.preview || "" })),
            unread_total: all.filter((m) => m.unread).length, query: "", folder: "sent" });
        }
        let list = term ? all.filter((m) => [m.subject, m.sender_name, m.sender, m.preview].some((v) => String(v || "").toLowerCase().includes(term)))
          : all.filter((m) => q.get("unread_only") !== "true" || m.unread);
        return json({ mail: list.map((m) => ({ folder: "Inbox", ...mailOut(m, [...MAILKEYS, "from_name", "to", "folder"]) })), unread_total: all.filter((m) => m.unread).length, query: q.get("q") || "" });
      }
      case "/mail/item": { const m = findMail(q.get("id")); return m ? json({ ...m, important: isImportant(m), body: m.body || `(Sent item)\n\n${m.subject}`, to: m.to, cc: m.cc || "" }) : fail(404, "That message isn't in the preview's sample data."); }
      case "/meetings": { const hours = Number(q.get("hours") || 24), end = new Date(Date.now() + hours * 3600000);
        const list = [...agenda, ...futureMeetings(1)].filter((m) => new Date(m.end) > new Date() && new Date(m.start) < end).sort((a, b) => a.start.localeCompare(b.start));
        return json({ meetings: list }); }
      case "/meetings/prep": {
        const m = [...agenda, ...futureMeetings(1)].find((x) => x.id === q.get("id")) || agenda[2];
        return json({ subject: m.subject, location: m.location, organizer: m.organizer, join_url: m.join_url, start: m.start, end: m.end,
          attendees: [{ name: m.organizer, email: "", response: "organizer" }, { name: ME.name, email: ME.email, response: "accepted" },
            { name: "Avery Chen", email: P.avery[1], response: "accepted" }, { name: "Sam Patel", email: P.sam[1], response: "tentative" },
            { name: "Tom Becker", email: P.tom[1], response: "no response", optional: true }],
          recent_mail: [pick(M.po, ["id", "subject", "sender_name", "received", "folder"]), pick(M.freight, ["id", "subject", "sender_name", "received", "folder"]),
            { ...sentMail[0], folder: "Sent" }],
          agenda: "1. Ship dates for PO 4410287\n2. Freight quote for Harbor Point\n3. Back-order plan for the amplifiers" });
      }
      case "/search": { const term = (q.get("q") || "").toLowerCase();
        return json({ mail: allMail().filter((m) => `${m.subject} ${m.sender_name || ""} ${m.to || ""}`.toLowerCase().includes(term)).slice(0, 20)
          .map((m) => pick(m, ["id", "subject", "sender_name", "received", "folder", "to"])) }, 200, 300); }
      case "/websites": return json({ monitors: monitors() });
      case "/sql": return json(sql);
      case "/sql/tables": return json({ tables: q.get("connection") === "warehouse" ? ["dbo.Bins", "dbo.Pickers", "dbo.Shipments", "dbo.Waves"] : TABLES });
      case "/sql/preview": return json({ ...orderRows, seconds: 0.08 });
      case "/orders/status": return json({ pattern: "\\b(\\d{7,10})\\b", problem: null });
      case "/orders/lookup": { const n = q.get("number"); const rows = orderRows.rows.filter((r) => r[0].replace(/^0+/, "") === String(n).replace(/^0+/, ""));
        return json({ number: n, query: "Find order", columns: orderRows.columns, rows, truncated: false, seconds: 0.12 }, 200, 250); }
      case "/teams": return json({ set_up: true, url: "https://teams.microsoft.com/v2/", state: teamsState(), read_at: at(-2), recent: teamsRecent(),
        activity: Object.values(T).map((m) => ({ id: m.id, subject: m.subject, received: m.received, unread: m.unread })) });
      case "/projects": return json({ projects });
      case "/vscode/status": return json({ status: "stopped", detail: "", log: [], mirror: null });
      case "/vscode/url": return fail(409, `VS Code web runs only in the Command Center desktop app. ${DESKTOP_ONLY}`);
      case "/pulse/status": return json(pulseStatus());
      case "/pulse/summary": return json(pulseSummary());
      case "/pulse/tickets": {
        const mine = q.get("mine") === "true", status = q.get("status") || "open", s = (q.get("search") || "").toLowerCase().replace(/^#/, "");
        let list = tickets.filter((t) => (status === "all" || t.status !== "Resolved") && (!mine || t.mine));
        if (s) list = list.filter((t) => `${t.ticket_no} ${t.title} ${t.detail} ${t.requester}`.toLowerCase().includes(s));
        return json({ configured: true, tickets: list.sort((a, b) => b.created_at.localeCompare(a.created_at)) });
      }
      case "/window/pulse": return json({ available: false, sites: { prod: "https://pulse.example.com/", dev: "https://dev.pulse.example.com/" }, views: {} });
      case "/window/ssms": return json({ available: false });
      case "/window/code": return json({ available: false });  // the docked desktop VS Code: desktop app only
      case "/window/teams": return json({ available: false, views: {} });
      case "/people/important": return json(importantState());
      case "/window/state": return json({ fullscreen: false });
      case "/followups/urgent": return json({ people: urgentPeople, from_config: ["Lena Ortiz"], internal_domains: ["example.com"] });
      case "/followups/muted": return json({ muted });
      case "/snoozes": return json({ snoozes, reply_watches: watches });
      case "/planner": return json(planner(Number(q.get("days") || 10)));
      case "/feeds": return json({ feeds: vaultStatus().available ? feeds : [], colors: FEED_COLORS });
      case "/profile": return json({ profile, presets: PRESETS, defaults: PROFILE_DEFAULTS });
      case "/vault/status": return json(vaultStatus());
      case "/privacy/inventory": return json({ items: inventory(), vault: vaultStatus(), settings: { lock_on_windows_lock: true, auto_lock_minutes: 240 } });
      case "/reminders": return json(remindersData());
      case "/reminders/active": return json({ items: demoPop.map((i) => (vaultStatus().available ? i : { ...i, text: undefined, note: undefined })),
        snooze_minutes: 15, available: vaultStatus().available, privacy: vault.privacy, next_wake: null });
      case "/offhours": return json(offhours());
      case "/daily/uncommitted": return json(uncommitted(), 200, q.get("refresh") ? 400 : 40);
      case "/daily/recap": return json(recap(q.get("day")), 200, 250);
      case "/daily/notes": { const s = (q.get("q") || "").toLowerCase();
        const list = [...notes.values()].filter((n) => !s || `${n.subject} ${n.notes}`.toLowerCase().includes(s)).map((n) => ({ id: n.id, start: n.start, end: n.end, subject: n.subject,
          preview: n.notes.replace(/\s+/g, " ").slice(0, 120), actions: n.actions.length, open: n.actions.filter((a) => !a.done).length }));
        return json({ keys: noteKeys(), notes: list }); }
      case "/daily/notes/item": { const n = notes.get(`${q.get("id")}|${q.get("start")}`);
        return json(n ? { exists: true, ...n } : { exists: false, notes: "", actions: [] }); }
      case "/spam/blocks": return json({ blocks, internal_domains: ["example.com"], rule: { ok: true, checked: at(-30), local_only: false },
        rule_names: ["Command Center spam blocks", "Command Center spam blocks (subjects)"] });
      case "/spam/options": { const m = findMail(q.get("id")); if (!m) return fail(404, "That message isn't in the sample data.");
        const domain = m.sender.split("@").pop();
        return json({ sender: m.sender, sender_name: m.sender_name, domain, subject: m.subject, phrase: m.subject.replace(/^(re|fw|fwd):\s*/i, "").slice(0, 60),
          kinds: { sender: { ok: true }, domain: { ok: true }, subject: { ok: true } } }); }
      case "/toolkit/settings": return json(tkMeta());
      case "/toolkit/changes": { const t = q.get("table"); return json({ tables: Object.fromEntries(Object.entries(TK_TABLES).map(([k, v]) => [k, v.label])),
        changes: tkChanges.filter((c) => !t || c.table_key === t) }); }
    }
    return null;
  }

  function post(path, b) {
    switch (path) {
      case "/alerts/read-mail": { let n = 0; alerts.forEach((a) => { if (!a.read && a.action?.type === "mail" && a.action.id === b.id) { a.read = 1; n++; } }); return json({ ok: true, alerts_read: n }); }
      case "/alerts/read": alerts.forEach((a) => { if (b.id == null || a.id === b.id) a.read = 1; }); return json({ ok: true });
      case "/alerts/clear": alerts = []; return json({ ok: true });
      case "/reload": return json({ ok: true });
      case "/focus": { const m = Number(b.minutes) || 0; focus = m > 0 ? { active: true, until: at(m), reason: "manual" } : { active: false }; return json(focus); }
      case "/open": toast(`Preview: the desktop app opens ${/^msteams:/.test(b.url || "") ? "the Teams app" : hostOf(b.url)} here.`); return json({ ok: true });
      case "/outlook/open": toast("Preview: the desktop app opens this item in Outlook."); return json({ ok: true });
      case "/tasks": { const t = { source: b.source === "asana" ? "asana" : "outlook", id: b.source === "asana" ? String(1205550100 + seq++) : hid(), subject: String(b.subject || "").trim(),
        due: b.due || null, priority: b.priority || "normal" };
        if (t.source === "asana") { t.url = `https://app.asana.com/0/0/${t.id}`; t.project = "My tasks"; }
        tasks.push(t); return json({ ok: true, task: t }); }
      case "/tasks/complete": tasks = tasks.filter((t) => t.id !== b.id);
        for (const m of Object.values(M)) if (m.id === b.id) m.flagged = false;
        return json({ ok: true });
      case "/tasks/priority": { const t = tasks.find((x) => x.id === b.id); if (t) t.priority = b.priority; return json({ ok: true }); }
      case "/calendar/block": { const s = new Date(b.start); const e = new Date(s.getTime() + (Number(b.minutes) || 30) * 60000);
        if (ymd(s) === today()) agenda.push(mtg(b.subject, hm(s), hm(e), "", ME.name));
        return json({ ok: true }); }
      case "/snooze": { const m = findMail(b.id) || tasks.find((t) => t.id === b.id); snoozes[b.id] = { until: b.until, subject: b.subject || m?.subject || "", who: b.who || "", kind: b.kind || "mail" };
        return json({ ok: true }); }
      case "/unsnooze": delete snoozes[b.id]; return json({ ok: true });
      case "/reply-watch": { const m = findMail(b.id); watches.push({ id: b.id, subject: m?.subject || "", to: m?.to || "", due: dayAt(Number(b.days) || 2, 9), conversation: `conv-${seq++}` }); return json({ ok: true }); }
      case "/reply-watch/cancel": watches = watches.filter((w) => w.conversation !== b.conversation); return json({ ok: true });
      case "/followups/dismiss": dismissed.add(b.id); return json({ ok: true });
      case "/followups/mute": if (b.sender && !muted.includes(b.sender)) muted.push(b.sender); return json({ ok: true });
      case "/followups/unmute": muted = muted.filter((s) => s !== b.sender); return json({ ok: true });
      case "/followups/urgent": urgentPeople = [...new Set(["Lena Ortiz", ...(b.people || [])])]; return json({ ok: true });
      case "/mail/read": { const m = findMail(b.id); if (m) m.unread = !b.read; let n = 0; if (b.read) alerts.forEach((a) => { if (!a.read && a.action?.type === "mail" && a.action.id === b.id) { a.read = 1; n++; } }); return json({ ok: true, alerts_read: n }); }
      case "/mail/flag": { const m = findMail(b.id); if (m) m.flagged = !!b.flagged; return json({ ok: true }); }
      case "/mail/reply/preview": { const m = findMail(b.id) || {};
        const recips = [{ name: m.sender_name, email: m.sender, type: "To", external: m.internal === false }];
        if (b.reply_all && m.cc) recips.push({ name: m.cc, email: "dana.reyes@example.com", type: "CC", external: false });
        return json({ approval_id: `appr-${seq++}`, recipients: recips, subject: `RE: ${m.subject || ""}`, text: b.text, expires_in: 300 }, 200, 200); }
      case "/mail/reply": if (b.send) { replied.add(b.id); toast("Preview: nothing was sent (sample data)."); } return json({ ok: true }, 200, 200);
      case "/weather/select": weatherZip = b.zip === "92002" ? "92002" : "92001"; return json(weatherFor(weatherZip));
      case "/asana/token": asanaConnected = true; return json({ ok: true, name: ME.name });
      case "/asana/disconnect": asanaConnected = false; return json({ ok: true });
      case "/teams/check": case "/websites/check": return json({ ok: true }, 200, 600);
      case "/teams/login": case "/websites/login": toast("Preview: the desktop app opens a browser window to sign in."); return json({ ok: true });
      case "/sql/run": return json(b.query === "Late shipments this week"
        ? { columns: ["Shipment", "Customer", "Promised", "Carrier"], rows: [["SH-88120", "Fabrikam AV", day(-2), "ODFL"], ["SH-88131", "Northwind Audio", day(-1), "FEDEX"]], truncated: false, seconds: 0.34 }
        : { ...orderRows, rows: b.params?.[0] ? orderRows.rows.filter((r) => r.join(" ").includes(b.params[0])) : orderRows.rows }, 200, 250);
      case "/sql/test": return json({ columns: ["server_name", "database_name", "login_name"], rows: [["sql-demo01", "OrdersDemo", "cc_readonly"]], truncated: false, seconds: 0.05 });
      case "/sql/password": return json({ ok: true });
      case "/planner/personal": {
        if (!vaultStatus().available) return fail(423, "Unlock your personal vault first.");
        if (!String(b.title || "").trim()) return fail(400, "Give the entry a title.");
        const e = { id: b.id ?? personalSeq++, day: b.day, title: String(b.title).trim(), all_day: b.all_day !== false, start: b.all_day === false ? b.start : null,
          end: b.all_day === false ? b.end : null, notes: b.notes || "", updated: at(0) };
        personal = personal.filter((p) => p.id !== e.id).concat(e); return json({ ok: true, entry: e }); }
      case "/planner/personal/delete": personal = personal.filter((p) => p.id !== b.id); return json({ ok: true });
      case "/feeds": { const f = { id: `feed-${seq++}`, name: String(b.name || "Calendar").slice(0, 40), kind: /icloud/i.test(b.url || "") ? "icloud" : "google",
        color: b.color || FEED_COLORS[feeds.length % FEED_COLORS.length], enabled: true, detail: b.detail || "titles", count: 3, fetched: at(0), error: null };
        feeds.push(f); return json({ feed: f, upcoming: 3 }); }
      case "/feeds/update": { const f = feeds.find((x) => x.id === b.id); if (f) for (const k of ["enabled", "color", "detail"]) if (k in b) f[k] = b[k]; return json({ ok: true }); }
      case "/feeds/delete": feeds = feeds.filter((x) => x.id !== b.id); return json({ ok: true });
      case "/profile": try { return json({ profile: saveProfile(b.profile) }); } catch (e) { return fail(400, e.message); }
      case "/profile/reset": profile = { ...PROFILE_DEFAULTS }; return json({ profile });
      case "/privacy/mode": vault.privacy = !!b.on; if (b.on) vault.unlocked = false; return json({ vault: vaultStatus() });
      case "/vault/lock": vault.unlocked = false; return json({ status: vaultStatus() });
      case "/vault/unlock": if (!b.passphrase) return fail(400, "Enter your passphrase."); vault.unlocked = true; vault.privacy = false; return json({ status: vaultStatus(), moved: {} }, 200, 300);
      case "/vault/setup": vault = { ...vault, set_up: true, unlocked: true, privacy: false }; return json({ status: vaultStatus(), moved: {} });
      case "/vault/change": return json({ status: vaultStatus(), reencrypted: personal.length + feeds.length + reminders.length }, 200, 300);
      case "/vault/reset": personal = []; feeds = []; reminders = []; return json({ status: vaultStatus(), deleted: 12 });
      case "/reminders": {
        if (!vaultStatus().available) return fail(423, "Unlock your personal vault first.");
        if (!String(b.text || "").trim()) return fail(400, "Type what to remind you about.");
        const r = { id: b.id ?? remSeq++, text: String(b.text).trim(), note: b.note || "", time: b.time || "09:00", repeat: b.repeat || "weekdays", days: b.days || [],
          date: b.date || null, enabled: b.enabled !== false, snooze: b.snooze !== false };
        reminders = reminders.filter((x) => x.id !== r.id).concat(r).sort((a, c) => a.time.localeCompare(c.time)); return json({ ok: true, reminder: r }); }
      case "/reminders/delete": reminders = reminders.filter((r) => r.id !== b.id); return json({ ok: true });
      case "/reminders/done": b.done ? doneToday.add(b.id) : doneToday.delete(b.id); demoPop = demoPop.filter((i) => i.id !== b.id); return json({ ok: true });
      case "/reminders/snooze": demoPop = demoPop.filter((i) => i.id !== b.id); return json({ ok: true, wake: null });
      case "/daily/notes": { const k = `${b.id}|${b.start}`; const empty = !String(b.notes || "").trim() && !(b.actions || []).length;
        if (empty) { notes.delete(k); return json({ exists: false }); }
        notes.set(k, { id: b.id, start: b.start, end: b.end, subject: b.subject, notes: b.notes || "", actions: b.actions || [], updated: at(0) });
        return json({ exists: true, updated: at(0) }); }
      case "/daily/notes/delete": notes.delete(`${b.id}|${b.start}`); return json({ ok: true });
      case "/daily/notes/task": { const n = notes.get(`${b.id}|${b.start}`); if (!n || !n.actions[b.index]) return fail(404, "Save the action item first.");
        n.actions[b.index].task = { source: b.source, id: hid() };
        tasks.push({ source: b.source === "asana" ? "asana" : "outlook", id: n.actions[b.index].task.id, subject: n.actions[b.index].text, due: b.due || null, priority: "normal" });
        return json({ actions: n.actions }); }
      case "/spam/preview": { const m = findMail(b.id); if (!m) return fail(404, "That message isn't in the sample data.");
        const pid = `pv-${seq++}`; const n = b.kind === "domain" ? 4 : b.kind === "subject" ? 2 : 3;
        const label = b.kind === "domain" ? `@${m.sender.split("@").pop()}` : b.kind === "subject" ? `subject “${b.value}”` : m.sender;
        previews.set(pid, { kind: b.kind, value: b.kind === "subject" ? b.value : b.kind === "domain" ? m.sender.split("@").pop() : m.sender, label, id: m.id });
        return json({ kind: b.kind, match_count: n, movable: n, exists: blocks.some((x) => x.label === label), preview_id: pid, expires_in: 300,
          sample: [{ subject: "Flash sale: shelving and racking", sender: m.sender, received: ago(30) }, { subject: "Your exclusive bulk discount", sender: m.sender, received: ago(80) }] }, 200, 250); }
      case "/spam/block": { const p = previews.get(b.preview_id); if (!p) return fail(409, "The preview expired. Check the matches again, then confirm.");
        const blk = { id: `blk-${blockSeq++}`, kind: p.kind, value: p.value, label: p.label, created: at(0) };
        blocks.push(blk); spammed.add(p.id);
        return json({ block: blk, moved: b.move_existing ? 3 : 0, trigger_moved: true, rule: { ok: true }, undo_seconds: 30 }, 200, 300); }
      case "/spam/undo": { const blk = blocks.find((x) => x.id === b.block_id); blocks = blocks.filter((x) => x.id !== b.block_id);
        if (blk) for (const m of allMail()) if (m.sender === blk.value || m.sender?.endsWith(`@${blk.value}`)) spammed.delete(m.id);
        return json({ unblocked: true, moved_back: 1 }); }
      case "/spam/unblock": blocks = blocks.filter((x) => x.id !== b.id); return json({ ok: true });
      case "/spam/sync": return json({ ok: true });
      case "/vscode/start": case "/vscode/sync": case "/vscode/claude": return fail(409, `VS Code web runs only in the Command Center desktop app. ${DESKTOP_ONLY}`);
      case "/vscode/stop": return json({ status: "stopped" });
      case "/projects/launch": case "/projects/new": case "/app/restart": case "/app/quit": case "/session/lock":
      case "/window/fullscreen": case "/window/minimize": case "/window/hide": return fail(409, DESKTOP_ONLY);
      case "/window/pulse/action": case "/window/ssms/action": case "/window/teams/action": return fail(409, DESKTOP_ONLY);
      case "/people/important": {
        let list = Array.isArray(b.senders) ? b.senders.slice() : importantSenders.slice();
        if (Array.isArray(b.remove)) { const drop = new Set(b.remove.map((r) => String(r).trim().toLowerCase())); list = list.filter((e) => !drop.has(e)); }
        if (b.add != null) list.push(b.add);
        const out = [];
        for (const raw of list) {
          const e = String(raw || "").trim().toLowerCase();
          if (!e) continue;
          if (e.startsWith("@")) {
            const d = e.slice(1);
            if (!/^[a-z0-9.-]+\.[a-z]{2,}$/.test(d)) return fail(400, `Not an address or @domain: ${raw}`);
            if (d === "example.com") return fail(400, "Whole internal domains can't be important senders: pick the people.");
            if (SHARED_PROVIDERS.includes(d)) return fail(400, `@${d} is a shared mail provider: add the address instead.`);
          } else if (!/^[^@\s]+@[^@\s]+\.[a-z]{2,}$/.test(e)) return fail(400, `Not an address or @domain: ${raw}`);
          if (!out.includes(e)) out.push(e);
        }
        if (out.length > 200) return fail(400, "At most 200 important senders.");
        importantSenders = out;
        return json(importantState());
      }
      case "/pulse/setup": return json({ ok: true, configured: true });
      case "/pulse/disconnect": return json({ ok: true });
      // Toolkit: read calls answer from the sample tables; every change is refused (the confirmation still shows).
      case "/toolkit/settings": return json(tkMeta());
      case "/toolkit/shipping/accounts": return json({ rows: shipAccounts, truncated: false }, 200, 200);
      case "/toolkit/shipping/methods": return json({ rows: shipMethods[b.account_id] || [] }, 200, 150);
      case "/toolkit/sps/search": return json({ rows: [{ spsID: 9300418, documentTimestamp: `${day(-1)} 14:22:05` }, { spsID: 9300377, documentTimestamp: `${day(-2)} 09:10:44` },
        { spsID: 9299860, documentTimestamp: `${day(-6)} 16:48:19` }] }, 200, 300);
      case "/toolkit/sps/carrier": return json({ rows: [{ CarrierAlphaCode: "FDEG", CarrierRouting: "FEDEX GROUND" }] }, 200, 200);
      case "/toolkit/sps/check": return json({ results: { [b.CarrierAlphaCode]: [{ SHIPPING_METHOD_ID: 311, ACCOUNT_ID: 101, INBOUND_ID: "FDEG", SHIP_VIA: "FEDEX GROUND" }], [b.CarrierRouting]: [] } }, 200, 200);
      case "/toolkit/logi/all": return json({ users: { rows: logiUsers }, dash: { rows: logiDash } }, 200, 250);
      case "/toolkit/logi/password": return json({ password: "Sample-Only-7Kq#" });
      case "/toolkit/logi/user": return json(userTables(String(b.user_id || "")));
      case "/toolkit/tree": return json(tree, 200, 200);
      case "/toolkit/test": return json({ login: "cc_toolkit", server: "sql-demo01", database: "STAGINGDB" }, 200, 300);
      case "/toolkit/password": return json({ test: { login: "cc_toolkit" } }, 200, 300);
      case "/toolkit/cert/inspect": { const c = tkMeta().cert_pin;
        return json({ cert: { host: "sql-demo01", port: 1433, subject: c.subject, issuer: c.subject, valid_from: "2025-03-01", valid_to: c.valid_to, sha256: c.sha256, self_signed: true }, pin: c, matches: true }); }
      case "/toolkit/cert/pin": case "/toolkit/cert/unpin": case "/toolkit/forget": return fail(409, DESKTOP_ONLY);
    }
    if (path.startsWith("/toolkit/rows/")) return fail(409, "Not applied: this preview never changes data. In the desktop app the change is applied in one transaction and logged.");
    return null;
  }

  function planner(n) {
    const days = [];
    const ft = followups();
    for (let i = 0; i < n; i++) {
      const date = day(i);
      const items = [];
      for (const t of openTasks()) {
        const due = t.due && t.due < day(0) ? day(0) : t.due;
        if (due === date) items.push({ kind: "task", id: t.id, title: t.subject, source: t.source === "asana" ? "asana" : "todo", day: date, overdue: !!t.due && t.due < day(0), url: t.url || null });
      }
      ft.flagged.forEach((f) => { const due = f.due && f.due < day(0) ? day(0) : f.due; if (due === date) items.push({ kind: "flag", id: f.id, title: f.subject, source: "flagged", day: date, overdue: f.due < day(0), who: f.sender_name }); });
      for (const [id, s] of Object.entries(snoozes)) if (String(s.until).slice(0, 10) === date) items.push({ kind: "snooze", id, title: s.subject, source: s.kind === "task" ? "todo" : "outlook", day: date, time: String(s.until).slice(11, 16), who: s.who });
      for (const w of watches) if (String(w.due).slice(0, 10) === date) items.push({ kind: "reply-watch", id: w.id, title: w.subject, source: "sent", day: date, time: String(w.due).slice(11, 16), who: `to ${w.to}` });
      days.push({ date, meetings: i === 0 ? agenda.slice() : futureMeetings(i), overlays: vaultStatus().available ? overlayEvents(i) : [], items,
        personal: vaultStatus().available ? personal.filter((p) => p.day === date) : [] });
    }
    const v = vaultStatus();
    return { days, personal_state: { available: v.available, set_up: v.set_up, unlocked: v.unlocked, privacy: v.privacy, waiting: 0 },
      calendars: feeds.map((f) => ({ id: f.id, error: f.error })), outlook_error: null };
  }

  // ---- fetch -------------------------------------------------------------------------------------------------------
  window.fetch = async (input, opts = {}) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, location.href);
    const m = /\/api(\/.*)$/.exec(url.pathname);
    if (!m) return new Response("Not available in the preview", { status: 404 });  // nothing goes to the network
    const method = String(opts.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    let body = {};
    try { body = opts.body ? JSON.parse(opts.body) : {}; } catch { body = {}; }
    const path = m[1].replace(/\/+$/, "") || "/";
    const res = method === "GET" ? get(path, url.searchParams) : /\/window\/[a-z]+\/bounds$/.test(path) ? json({ ok: true }) : post(path, body);
    if (res) return res;
    window.__ccPreview.unknown.push(`${method} ${path}`);
    return method === "GET" ? fail(404, "Not in the preview's sample data.") : json({ ok: true });
  };

  // ---- preview extras -----------------------------------------------------------------------------------------------
  document.addEventListener("cc:ready", () => {
    window.CC.addPaletteSource(() => [{ group: "Commands", label: "Preview: show a personal reminder pop-up", hint: "demo of the reminder dialog",
      run: () => { demoPop = [{ id: 1, day: today(), at: at(0), text: "Stretch and refill water", note: "Two minutes away from the screen", snooze: true, missed: false }];
        window.dispatchEvent(new Event("focus")); } }]);
  }, { once: true });
  const wireNote = () => {
    const note = document.getElementById("preview-note");
    try { if (sessionStorage.getItem("cc.preview.note") === "hidden") note.hidden = true; } catch { /* storage blocked */ }
    document.getElementById("preview-note-x")?.addEventListener("click", () => {
      note.hidden = true;
      try { sessionStorage.setItem("cc.preview.note", "hidden"); } catch { /* storage blocked */ }
      window.dispatchEvent(new Event("resize"));
    });
  };
  document.getElementById("preview-note") ? wireNote() : document.addEventListener("DOMContentLoaded", wireNote, { once: true });
})();
