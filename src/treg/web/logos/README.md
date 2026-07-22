# Provider logos

Each file is named for its `service` in `oauth_providers.py`, so the dashboard resolves one by
convention (`/logos/<service>.svg`) with no registry field to keep in sync.

These are third-party trademarks, used nominatively to identify which service a connection talks
to. They are not treg's marks and imply no endorsement by their owners. Sourced from each brand's
own published icon (Google's product-logo CDN, the services' favicons) and hand-traced to SVG.

Every mark is the **light-surface form** — the dashboard renders each one inside a uniform
near-white tile, in both themes. That is deliberate: brand marks are designed against white, they
vary enormously in ink coverage (LinkedIn is a full-bleed square, Google Analytics is three thin
bars), and monochrome marks like X and TikTok are invisible on one of our two themes if left bare.
One shared surface fixes all three at once, and keeps the column optically even.
