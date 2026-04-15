import { useEffect } from "react";

// In the future, query parameters from the incoming URL (e.g., ?ref=share&exerciseId=123)
// could be preserved by encoding them into the App Store URL's "pt" or "ct" parameters,
// or by using a deferred deep linking service.
const APP_STORE_URL = "https://apps.apple.com/us/app/lift-the-bull/id6759113833";

export default function AppRedirect() {
  useEffect(() => {
    window.location.replace(APP_STORE_URL);
  }, []);

  return (
    <div style={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: "60vh", color: "#aaa" }}>
      <p>Redirecting to the App Store...</p>
    </div>
  );
}
