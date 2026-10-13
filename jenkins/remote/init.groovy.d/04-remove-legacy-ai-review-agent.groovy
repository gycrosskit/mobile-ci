import jenkins.model.Jenkins

import java.nio.file.Files
import java.nio.file.Paths

def jenkins = Jenkins.get()
def legacyNode = jenkins.getNode('ai-review-linux')
if (legacyNode != null) {
    jenkins.removeNode(legacyNode)
    println("Removed legacy Jenkins node 'ai-review-linux'.")
}

def legacyScript = Paths.get('/var/jenkins_home/init.groovy.d/04-configure-ai-review-agent.groovy')
try {
    if (Files.deleteIfExists(legacyScript)) {
        println("Removed legacy Jenkins init script '${legacyScript}'.")
    }
} catch (Exception error) {
    println("Could not remove legacy Jenkins init script '${legacyScript}': ${error.class.simpleName}")
}
