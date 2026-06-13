import AppStoreButton from "../components/AppStoreButton";
import AndroidInterestButton from "../components/AndroidInterestButton";
import { BullIcon } from "../components/BullIcon";

function Home() {
  return (
    <div className="home">
      <section className="hero">
        <div className="hero-bull">
          <BullIcon size={96} color="#FFC850" />
        </div>
        <h1 className="hero-title">Lift the Bull</h1>
        <p className="hero-eyebrow">Strength Tracker</p>
        <p className="tagline">
          Skip the noise. Train what matters. Get measurably stronger.
        </p>
        <div className="hero-cta">
          <AppStoreButton size="hero" />
          <AndroidInterestButton size="hero" />
        </div>
      </section>

      <section className="features">
        <div className="feature-card">
          <h3>Know Your Strength</h3>
          <p>
            Every lift shows its current estimated 1RM and strength tier. No
            spreadsheets, no guessing — just an honest read of where you stand
            today.
          </p>
        </div>
        <div className="feature-card">
          <h3>Unlock The Next Tier</h3>
          <p>
            Concrete, predefined milestones replace open-ended programming.
            Hit the target, level up your tier, push your strength forward.
          </p>
        </div>
        <div className="feature-card">
          <h3>See Your Progress</h3>
          <p>
            Glance at what you did last time. Watch your strength grow week
            over week. Always know you're moving forward.
          </p>
        </div>
      </section>

      <style>{`
        .hero {
          text-align: center;
          padding: 4rem 0 3rem;
          display: flex;
          flex-direction: column;
          align-items: center;
        }
        .hero-bull {
          margin-bottom: 1.5rem;
          display: inline-flex;
        }
        .hero-title {
          font-size: 4.5rem;
          letter-spacing: 6px;
          margin-bottom: 0.4rem;
        }
        .hero-eyebrow {
          font-family: var(--font-body);
          font-size: 0.78rem;
          font-weight: 600;
          letter-spacing: 4px;
          text-transform: uppercase;
          color: var(--color-accent);
          margin-bottom: 1.5rem;
        }
        .tagline {
          font-size: 1.15rem;
          color: var(--color-text-secondary);
          max-width: 560px;
          margin: 0 auto 2rem;
          line-height: 1.55;
        }
        .hero-cta {
          display: inline-flex;
          flex-wrap: wrap;
          justify-content: center;
          gap: 12px;
          margin-top: 0.25rem;
        }
        .features {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
          gap: 1.5rem;
          padding: 2rem 0 4rem;
        }
        .feature-card {
          background-color: rgba(255, 255, 255, 0.06);
          background-image: linear-gradient(
            180deg,
            rgba(255, 255, 255, 0.04) 0%,
            transparent 60%
          );
          border: 1px solid rgba(255, 255, 255, 0.10);
          border-radius: var(--radius-card);
          padding: 1.75rem 1.5rem 1.5rem;
        }
        .feature-card h3 {
          color: var(--color-accent);
          font-size: 1.4rem;
          margin-top: 0;
          margin-bottom: 0.6rem;
        }
        .feature-card p {
          color: var(--color-text-secondary);
          font-size: 0.95rem;
          line-height: 1.55;
        }
        @media (max-width: 600px) {
          .hero-title {
            font-size: 3.25rem;
            letter-spacing: 4px;
          }
          .hero {
            padding: 2.5rem 0 2rem;
          }
        }
      `}</style>
    </div>
  );
}

export default Home;
