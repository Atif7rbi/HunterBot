import { createBinanceFeed } from './binance-feed.js';
import { createMarketEngine } from './market-engine.js';
import { createUIRenderer } from './ui-renderer.js';

const feed = createBinanceFeed();
const engine = createMarketEngine();
const ui = createUIRenderer(document);

async function bootstrap() {
  ui.renderLoading('Initializing Binance feed...');
  await feed.init();
  ui.renderLoading('Feed initialized. Waiting for snapshot...');

  feed.subscribe((snapshot) => {
    const analysis = engine.analyze(snapshot);
    ui.render(snapshot, analysis);
  });

  ui.renderLoading('Dashboard is live. Waiting for first engine cycle...');
  feed.start();
}

bootstrap().catch((err) => {
  console.error(err);
  ui.renderError('Failed to bootstrap Dashboard module.');
});
