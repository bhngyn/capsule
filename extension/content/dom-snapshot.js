// Click-time DOM snapshot.
//
// Captures `document.documentElement.outerHTML` plus a small structural
// summary (counts of nodes, iframes, videos, images visible/total). This
// is the "what the user was actually looking at" record, distinct from
// the Playwright MHTML the backend produces — the user's browser may
// render differently due to extensions, content blockers, OS fonts, etc.
//
// Returned shape:
//   {
//     outer_html: <string>,
//     meta: {
//       captured_at, page_url, document_title, ready_state,
//       counts: { nodes, iframes, videos, images_total, images_visible },
//       scroll: { x, y },
//       viewport: { width, height, device_scale_factor }
//     }
//   }

(() => {
  function countNodes(root) {
    let n = 0;
    const walker = document.createTreeWalker(
      root,
      NodeFilter.SHOW_ELEMENT,
      null,
    );
    while (walker.nextNode()) n++;
    return n;
  }

  const imgs = Array.from(document.images || []);
  let visible = 0;
  for (const img of imgs) {
    const r = img.getBoundingClientRect();
    if (r.top < window.innerHeight && r.bottom > 0 && r.width > 0 && r.height > 0) {
      visible++;
    }
  }

  return {
    outer_html: document.documentElement ? document.documentElement.outerHTML : "",
    meta: {
      captured_at: new Date().toISOString(),
      page_url: location.href,
      document_title: document.title,
      ready_state: document.readyState,
      counts: {
        nodes: countNodes(document.documentElement || document),
        iframes: document.querySelectorAll("iframe").length,
        videos: document.querySelectorAll("video").length,
        images_total: imgs.length,
        images_visible: visible,
      },
      scroll: { x: window.scrollX || 0, y: window.scrollY || 0 },
      viewport: {
        width: window.innerWidth,
        height: window.innerHeight,
        device_scale_factor: window.devicePixelRatio || 1,
      },
    },
  };
})();
