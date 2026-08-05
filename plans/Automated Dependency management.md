# Automated Dependency management 

* Extract all the “git.url” values from all the hierarchy and levels from this yaml [https://github.com/opendatahub-io/odh-build-metadata/blob/main/components/odh-operator/64dd0bc9ee2d664fe1e8a439a28d0171d35c8ed3dadaa543acfc4670c11036a5/manifests-config.yaml](https://github.com/opendatahub-io/odh-build-metadata/blob/main/components/odh-operator/64dd0bc9ee2d664fe1e8a439a28d0171d35c8ed3dadaa543acfc4670c11036a5/manifests-config.yaml)  
* Explore each github repo and find out  
  * If there is already a tool like renovate or dependabot or any other tool configured on the repo to automatically update the pakages & dependencies to their new versions  
  * If yes the does it the tool just raise the PR or auto-merges the PR as well  
  * What’s the cooldown period used by the tool for fetching the latest version of dependencies

