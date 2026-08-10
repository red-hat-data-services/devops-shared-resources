How can we create a github ruleset and use other github constructs in such a way that:

* Manual pushes to a given branches are totally blocked to a given branch of a given repo for a given time period  
* We should be able to have a github workflow which decides whether current time falls under the restricted time period and also checks few other prerequisites  
* All the changes should go through PRs and PR should only be able to be merged using a github-app bot from the mentioned github workflow  
* Nobody else should be able to merge the PR even if they have all the access on the repo

Can you create a reusable post-codefreeze-gatekeeper github workflow with following requirements

* It will be used to run as a CI check on PRs  
* It should only validate the PRs to the release branches where name of the branch would follow the pattern \- "rhoai-x.y" or "rhoai-x.y-ea.n"  
  * Here x, y and n are numbers ranging from 0 to 9  
  * Using this we can devise the RHOAI-jira-version   
    * For "rhoai-x.y", jira version should be "x.y GA RHOAI RELEASE"  
    * For "rhoai-x.y-ea.n" jira version should be "x.y EAn RHOAI RELEASE"  
* It should execute following checks  
  * A jira issue is attached in the PR description  
  * The jira issue belongs to either RHOAIENG or RHAIENG jira project  
  * The jira issue has target version and fix version set as identified RHOAI-jira-version  
  * The value of custom jira field "Release Blocker" is set as "Approved"  
* If any of the mentioned criteria doesn't meeting then fail with appropriate error message  
* Try to execute as many checks as possible even if previous check failed to ensure we report all the possible issues in one shot