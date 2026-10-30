/*
 * ShelfLife: export your Steam Family Sharing library to a JSON file.
 *
 * Steam's public Web API hides games you can play through a Steam Family, so
 * this runs inside YOUR logged-in browser, the same way SteamDB's extension does.
 *
 * How to use
 *   1. Log in at https://store.steampowered.com in your browser.
 *   2. On that page, open DevTools (F12) > Console, paste this whole file, press Enter.
 *      (Chrome/Edge may ask you to type "allow pasting" first.)
 *   3. A file named family-library.json downloads. Move it into ShelfLife's data/ folder.
 *   4. Run:  python -m scripts.import_family
 *
 * What it does
 *   - Asks the Steam store for a short-lived web API token (using your login cookies).
 *   - Calls Steam's IFamilyGroupsService/GetSharedLibraryApps with that token.
 *   - Saves the app list. The token is kept only in this script's memory: it is
 *     never printed, never written to the file, and never sent anywhere else.
 *
 * Notes
 *   - These are undocumented Valve endpoints and may change without notice.
 *   - The export lists which games your family members own (by SteamID). Keep the
 *     file private; do not post it or commit it to git (data/ is git-ignored).
 *   - Only run scripts like this that you have read and understand.
 */
(async () => {
  try {
    if (location.origin !== 'https://store.steampowered.com') {
      console.error('ShelfLife: open https://store.steampowered.com first, then run this again.');
      return;
    }

    const configResponse = await fetch('/pointssummary/ajaxgetasyncconfig', {
      credentials: 'include',
      headers: { Accept: 'application/json' },
    });
    const config = await configResponse.json();
    const token = config && config.success && config.data && config.data.webapi_token;
    if (!token) {
      console.error('ShelfLife: no token returned. Are you logged in to the Steam store in this browser?');
      return;
    }

    const params = new URLSearchParams({
      access_token: token,
      family_groupid: '0', // ignored by Steam; the token identifies your family
      include_excluded: 'true',
      include_free: 'true',
      include_non_games: 'true',
      include_own: 'true',
    });

    let response;
    try {
      response = await fetch('https://api.steampowered.com/IFamilyGroupsService/GetSharedLibraryApps/v1/?' + params);
    } catch (error) {
      console.error('ShelfLife: the request to Steam was blocked by the browser:', error.message);
      return;
    }
    if (!response.ok) {
      console.error('ShelfLife: Steam answered HTTP ' + response.status + '. Is this account in a Steam Family?');
      return;
    }

    const body = await response.json();
    const apps = body.response && body.response.apps;
    if (!Array.isArray(apps)) {
      console.error('ShelfLife: unexpected response from Steam; nothing exported.');
      return;
    }

    const output = {
      format: 1,
      exported_at: new Date().toISOString(),
      owner_steamid: body.response.owner_steamid ? String(body.response.owner_steamid) : null,
      apps: apps.map((app) => {
        const entry = {
          appid: app.appid,
          owner_steamids: (app.owner_steamids || []).map(String),
          exclude_reason: app.exclude_reason ?? 0,
        };
        if (app.name) entry.name = app.name;
        return entry;
      }),
    };

    const blob = new Blob([JSON.stringify(output, null, 1)], { type: 'application/json' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'family-library.json';
    document.body.appendChild(link);
    link.click();
    link.remove();
    console.log('ShelfLife: exported ' + output.apps.length + ' apps to family-library.json');
  } catch (error) {
    console.error('ShelfLife: export failed:', error);
  }
})();
