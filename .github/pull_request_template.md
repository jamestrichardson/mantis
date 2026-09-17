## Summary

<!-- What does this PR change, and why? Keep this focused on the problem and outcome. -->

## Related issue

<!-- Use "Closes #123" when this PR fully resolves an issue, or "Related to #123" otherwise. -->

## Design / scope notes

<!-- Call out important architectural decisions, tradeoffs, and explicit non-goals. Remove this section if not needed. -->

## Test plan

<!-- List the deterministic checks you ran. Do not include secrets or sensitive infrastructure details in pasted output. -->

- [ ] `pytest`
- [ ] Package/container checks run when relevant
- [ ] New or changed behavior has focused test coverage

## Documentation

- [ ] Documentation is updated for user/developer-visible behavior, or no documentation change is needed

## Safety / compatibility checklist

- [ ] No credentials, tokens, private keys, or sensitive environment values are included in this PR
- [ ] External/tool output remains bounded and follows Mantis's untrusted-evidence security boundary where applicable
- [ ] New remote calls have explicit timeout/reliability behavior where applicable
- [ ] Mutating behavior is not introduced without an explicit approval/policy design
- [ ] Deterministic CI does not require live AWX, LiteLLM, or another external service

## Release notes

<!-- Conventional Commit type drives release-please. Note anything operators should know about configuration, deployment, migration, or rollback. Write "None" if not applicable. -->
