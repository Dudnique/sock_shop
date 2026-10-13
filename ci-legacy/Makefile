# ARCHIVED - moved here from the repository root; see ci-legacy/README.md.
#
# This was the old Docker + make way of generating and checking
# deploy/kubernetes/complete-demo.yaml. Nothing runs it today:
#   - the check in CI is plain `awk | diff` (see .github/workflows/main.yaml);
#   - the stand is deployed by deploy.ps1 + Terraform, not by make.
# The `make -C deploy/kubernetes ...` paths used below no longer exist - the second
# Makefile (and the image it built) were archived next to this file as
# manifests-Makefile / manifests-Dockerfile.

.PHONY: gen-complete-demo
gen-complete-demo:
	make -C deploy/kubernetes docker-gen-complete-demo

.PHONY: check-generated-files
check-generated-files:
	make -C deploy/kubernetes docker-check-complete-demo
