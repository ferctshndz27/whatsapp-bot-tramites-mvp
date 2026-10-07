// CI local del bot de trámites en el Jenkins del equipo (job «tramites», http://127.0.0.1:8090).
//
// Complementa a GitHub Actions, no lo sustituye: Actions sigue corriendo test + build y es el único que
// promueve `production`; el VPS sigue jalando solo (deploy/pull-deploy.sh). Aquí corre, en cada commit
// local y antes de cualquier push, lo que Actions no corre —el juez de navegador del CRM y shellcheck—
// además de pytest y la imagen de producción. Sin credenciales: no publica ni despliega nada.
//
// Disparo: el hook post-commit (ci/instalar-hook.sh) avisa a Jenkins; si no, el escaneo de cada 2 min.
// Resultado: jenkinsctl result tramites <rama>   (jenkinsctl vive en 01-TOOLS/jenkins del harness).
//
// Reglas para tocar este archivo (skills jenkins-local y devops-senior):
//  - Declarativo; cada etapa con su agente. Imágenes fijadas por digest, nunca `latest`.
//  - Agentes en contenedor siempre con '--cpus=4 -m 4g': el equipo corre un modelo local.
//  - Sin --privileged, sin montar el socket de Docker ni rutas fuera del workspace, sin secretos.
//  - La lógica vive en ci/*.sh, que también se corren a mano; este archivo solo las orquesta.
// tests/test_ci_jenkins.py vigila las dos primeras y que ci/Dockerfile siga al Dockerfile.
pipeline {
  agent none

  options {
    timestamps()
    timeout(time: 30, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '20'))
    // Dos commits seguidos en la misma rama: se cancela el build viejo. En local solo importa el último.
    disableConcurrentBuilds(abortPrevious: true)
  }

  stages {
    stage('shellcheck') {
      agent {
        docker {
          image 'koalaman/shellcheck-alpine:v0.11.0@sha256:9955be09ea7f0dbf7ae942ac1f2094355bb30d96fffba0ec09f5432207544002'
          args '--cpus=4 -m 4g'
        }
      }
      steps {
        sh 'ci/shellcheck.sh'
      }
    }

    stage('pytest (Python 3.12)') {
      agent {
        dockerfile {
          filename 'ci/Dockerfile'
          additionalBuildArgs '--target pytest'
          args '--cpus=4 -m 4g'
        }
      }
      steps {
        sh 'rm -f reportes/pytest.xml && ci/pytest.sh --junitxml=reportes/pytest.xml'
      }
      post {
        always {
          junit allowEmptyResults: true, testResults: 'reportes/pytest.xml'
        }
      }
    }

    // Antes que el juez: tarda segundos (comparte la capa de pip con ci/Dockerfile) y un Dockerfile roto
    // se sabe sin esperar los minutos del navegador.
    stage('Imagen de producción') {
      agent any
      steps {
        sh 'ci/imagen.sh'
      }
    }

    stage('Juez de navegador (CRM)') {
      agent {
        dockerfile {
          filename 'ci/Dockerfile'
          additionalBuildArgs '--target navegador'
          // Chromium necesita más /dev/shm que los 64 MB que Docker da por defecto.
          args '--cpus=4 -m 4g --shm-size=1g'
        }
      }
      steps {
        sh 'rm -rf reportes/capturas && ci/navegador.sh --capturas=reportes/capturas'
      }
      post {
        failure {
          archiveArtifacts allowEmptyArchive: true, artifacts: 'reportes/capturas/*.png'
        }
      }
    }
  }
}
