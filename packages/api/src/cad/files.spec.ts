import os from 'node:os';
import yaml from 'js-yaml';
import path from 'node:path';
import express from 'express';
import request from 'supertest';
import files from 'node:fs/promises';
import { createCADController, configureCADDeployment } from './files';

describe('CAD export authorization', () => {
  const admin = 'a'.repeat(24);
  const user = 'b'.repeat(24);
  const reportError = jest.fn();
  let work: string;
  let app: express.Express;
  let workspaces: Record<string, { path: string; ownerIds: string[] }>;

  beforeEach(async () => {
    work = await files.mkdtemp(path.join(os.tmpdir(), 'cad-export-'));
    await Promise.all(['admin', 'user'].map((name) => files.mkdir(path.join(work, name))));
    await files.writeFile(path.join(work, 'admin', 'part.step'), 'admin geometry');
    await files.writeFile(path.join(work, 'user', 'part.step'), 'user geometry');
    workspaces = {
      admin: { path: path.join(work, 'admin'), ownerIds: [admin] },
      user: { path: path.join(work, 'user'), ownerIds: [user] },
    };
    const controller = createCADController({ files, reportError });
    app = express();
    app.use((req, _res, next) => {
      Object.assign(req, {
        user: { id: req.get('x-user-id') },
        config: {
          config: {
            cadExports: { workspaces },
          },
        },
      });
      next();
    });
    app.get('/', controller.list);
    app.get('/:filename', controller.download);
  });

  afterEach(async () => {
    jest.restoreAllMocks();
    reportError.mockClear();
    await files.rm(work, { recursive: true, force: true });
  });

  test('only the configured owner can list and download their workspace', async () => {
    expect((await request(app).get('/').set('x-user-id', admin)).body).toMatchObject([
      { filename: 'part.step' },
    ]);
    expect((await request(app).get('/part.step').set('x-user-id', admin)).body.toString()).toBe(
      'admin geometry',
    );
    expect((await request(app).get('/part.step').set('x-user-id', user)).body.toString()).toBe(
      'user geometry',
    );
    await request(app).get('/').expect(403);
    await request(app).get('/part.step').set('x-user-id', 'c'.repeat(24)).expect(403);
  });

  test('does not expose symlinks or unsupported paths', async () => {
    await files.symlink(
      process.platform === 'win32' ? path.join(work, 'user') : path.join(work, 'user', 'part.step'),
      path.join(work, 'admin', 'secret.step'),
      process.platform === 'win32' ? 'junction' : 'file',
    );
    const listing = await request(app).get('/').set('x-user-id', admin);
    expect(listing.body).toHaveLength(1);
    await request(app).get('/secret.step').set('x-user-id', admin).expect(404);
    await request(app).get('/..%2Fuser%2Fpart.step').set('x-user-id', admin).expect(404);
    await request(app).get('/config.env').set('x-user-id', admin).expect(404);
    await request(app).get('/part%00.step').set('x-user-id', admin).expect(404);
  });

  test('both authorized accounts can download the same shared file', async () => {
    workspaces = { shared: { path: path.join(work, 'admin'), ownerIds: [admin, user] } };
    for (const owner of [admin, user]) {
      expect((await request(app).get('/').set('x-user-id', owner)).body).toHaveLength(1);
      expect((await request(app).get('/part.step').set('x-user-id', owner)).body.toString()).toBe(
        'admin geometry',
      );
    }
    await request(app).get('/').set('x-user-id', 'c'.repeat(24)).expect(403);
    await request(app).get('/part.step').set('x-user-id', 'c'.repeat(24)).expect(403);
  });

  test('separates absence from an operational failure without disclosing diagnostics', async () => {
    await request(app).get('/missing.step').set('x-user-id', admin).expect(404);
    jest
      .spyOn(files, 'open')
      .mockRejectedValueOnce(Object.assign(new Error('private path and key'), { code: 'EACCES' }));
    const response = await request(app).get('/part.step').set('x-user-id', admin).expect(500);
    expect(response.body).toEqual({ code: 'CAD_EXPORT_UNAVAILABLE' });
    expect(JSON.stringify(reportError.mock.calls)).not.toContain('private path and key');
  });

  test('binds opaque user IDs without resolving unrelated provider secret placeholders', () => {
    const source =
      'cadExports:\n  workspaces:\n    shared:\n      path: /cad/shared\nendpoints:\n  custom:\n    - apiKey: "${OPENROUTER_API_KEY}"\n';
    const result = yaml.load(configureCADDeployment(source, { admin, user }));
    expect(result).toMatchObject({
      cadExports: { workspaces: { shared: { path: '/cad/shared', ownerIds: [admin, user] } } },
      endpoints: { custom: [{ apiKey: '${OPENROUTER_API_KEY}' }] },
      mcpSettings: {
        allowedAddresses: [
          `freecad-${admin}:8000`,
          `blender-${admin}:10101`,
          `freecad-${user}:8000`,
          `blender-${user}:10101`,
        ],
      },
    });
  });
});
