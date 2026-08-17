# Topic MOC notes are created only through the paper-notes CLI

The plugin may offer Create Topic MOC in the UI, but the file is written by
`paper-notes moc create`. That keeps the existing rule that managed vault
writes have one engine (validation, conflict, atomic create). The rejected
alternative was `vault.create` inside the plugin: shorter for v1, but it
would be the first second writer and would have to be re-done the moment
rows or cards become writable.
