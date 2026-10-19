chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== "JOBBEAST_CAPTURE_SCREENSHOT" || !sender.tab?.windowId) return false;
  chrome.tabs.captureVisibleTab(sender.tab.windowId, { format: "png" }, (dataUrl) => {
    sendResponse({ dataUrl: dataUrl || "" });
  });
  return true;
});
