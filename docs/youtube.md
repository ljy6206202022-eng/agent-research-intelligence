# Optional YouTube setup

Public discovery needs no account. For a user's subscription list, create a Google Desktop OAuth client in a project with YouTube Data API v3 enabled. Save its installed-app JSON as `secrets/youtube-client.json` in the owned workspace, set file mode `0600`, and do not commit it. The only requested scope is `https://www.googleapis.com/auth/youtube.readonly`.

The account adapter requires a local read-permission file at `config/permissions/youtube-readonly.json`. Create it only after deciding to enable account reads:

```json
{"status":"USER_APPROVED","scope":"https://www.googleapis.com/auth/youtube.readonly","account_mutation":false,"authorization_basis":"Explicit local user request","hosts":["accounts.google.com","oauth2.googleapis.com","www.googleapis.com"]}
```

Then run `research-intel youtube-account-authorize` in the owned workspace and complete the browser consent. The token is saved under ignored `secrets/` with restrictive permissions. `youtube-account-status` does not print credentials. `youtube-account-subscriptions` reads the account; `youtube-import-subscriptions --receipt <owned receipt>` can enter observed channels into the source registry. A subscription is not trust or accepted evidence.

Normal recent-video lookup uses the official uploads playlist API when authorized and a token is present, with public RSS as an uncredentialed path. L0 checks identifiers and cursors; L1 screens metadata; L2 reads light content; L3 acquires only a few selected items. Captions take priority. Description links can become independent evidence. Optional media and frames need their own dependencies and source rights. The project never subscribes or unsubscribes.
