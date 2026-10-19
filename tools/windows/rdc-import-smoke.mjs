import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

// Resolve from the global package, not from this supervision release.
const require = createRequire(pathToFileURL(resolve(process.argv[2], 'package.json')));
await import(pathToFileURL(require.resolve('@modelcontextprotocol/sdk/client/index.js')).href);
await import(pathToFileURL(require.resolve('@supabase/supabase-js')).href);
