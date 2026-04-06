import { Routes, Route, useSearchParams } from "react-router-dom";
import Navbar from "./components/Navbar";
import Footer from "./components/Footer";
import Home from "./pages/Home";
import Terms from "./pages/Terms";
import Privacy from "./pages/Privacy";
import Support from "./pages/Support";
import AppRedirect from "./pages/AppRedirect";

function App() {
  const [searchParams] = useSearchParams();
  const embedded = searchParams.get("embedded") === "1";

  return (
    <div className="app">
      {!embedded && <Navbar />}
      <main className="main-content">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/terms" element={<Terms />} />
          <Route path="/privacy" element={<Privacy />} />
          <Route path="/support" element={<Support />} />
          <Route path="/app" element={<AppRedirect />} />
        </Routes>
      </main>
      {!embedded && <Footer />}
    </div>
  );
}

export default App;
