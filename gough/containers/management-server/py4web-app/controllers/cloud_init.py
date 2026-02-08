"""
Cloud-Init Template Management Controller
"""

from py4web import action, request, abort, redirect, URL, response
from py4web.utils.form import Form, FormStyleBootstrap4
from ..models import db
from ..lib.cloud_init_processor import CloudInitProcessor, CloudInitValidator, TemplateRenderError
import json
import yaml
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

@action("cloud_init/templates")
@action.uses("cloud_init/templates.html", db)
def templates_list():
    """Cloud-init templates management page"""
    
    # Get filter parameters
    category_filter = request.query.get('category', '')
    search_query = request.query.get('search', '')
    
    # Build query
    query = db.cloud_init_templates
    
    if category_filter:
        query = query(db.cloud_init_templates.category == category_filter)
    
    if search_query:
        query = query(
            (db.cloud_init_templates.name.contains(search_query)) |
            (db.cloud_init_templates.description.contains(search_query))
        )
    
    templates = query.select(
        orderby=[db.cloud_init_templates.category, db.cloud_init_templates.name]
    )
    
    # Get unique categories for filter
    categories = db().select(
        db.cloud_init_templates.category,
        distinct=True,
        orderby=db.cloud_init_templates.category
    )
    
    return {
        'templates': templates,
        'categories': [row.category for row in categories if row.category],
        'current_filters': {
            'category': category_filter,
            'search': search_query
        }
    }

@action("cloud_init/template/create")
@action("cloud_init/template/edit/<template_id:int>")
@action.uses("cloud_init/template_form.html", db)
def template_form(template_id=None):
    """Create or edit cloud-init template"""
    
    template = None
    if template_id:
        template = db(db.cloud_init_templates.id == template_id).select().first()
        if not template:
            abort(404)
    
    # Create form
    form = Form([
        Field('name', 'string', length=255,
              requires=[IS_NOT_EMPTY(), IS_LENGTH(255)],
              default=template.name if template else ''),
        Field('description', 'text',
              default=template.description if template else ''),
        Field('category', 'string', length=100,
              requires=IS_LENGTH(100),
              default=template.category if template else ''),
        Field('template_content', 'text',
              requires=IS_NOT_EMPTY(),
              default=template.template_content if template else ''),
        Field('default_variables', 'json',
              default=template.default_variables if template else '{}',
              requires=IS_JSON()),
        Field('is_active', 'boolean',
              default=template.is_active if template else True)
    ], formstyle=FormStyleBootstrap4)
    
    if form.accepted:
        try:
            # Validate template
            processor = CloudInitProcessor()
            is_valid, errors = processor.validate_template(form.vars.template_content)
            
            if not is_valid:
                form.errors['template_content'] = 'Template validation failed: ' + '; '.join(errors)
            else:
                # Extract variables from template
                variables = processor.extract_variables(form.vars.template_content)
                
                data = {
                    'name': form.vars.name,
                    'description': form.vars.description,
                    'category': form.vars.category,
                    'template_content': form.vars.template_content,
                    'default_variables': form.vars.default_variables,
                    'template_variables': json.dumps(variables),
                    'is_active': form.vars.is_active,
                    'updated_on': datetime.utcnow()
                }
                
                if template:
                    # Update existing template
                    db(db.cloud_init_templates.id == template_id).update(**data)
                    logger.info(f"Updated cloud-init template: {form.vars.name}")
                else:
                    # Create new template
                    data['created_on'] = datetime.utcnow()
                    template_id = db.cloud_init_templates.insert(**data)
                    logger.info(f"Created cloud-init template: {form.vars.name}")
                
                db.commit()
                redirect(URL('cloud_init/template/detail', template_id))
                
        except Exception as e:
            logger.error(f"Failed to save template: {e}")
            form.errors['general'] = f'Failed to save template: {str(e)}'
    
    return {
        'form': form,
        'template': template,
        'is_edit': template is not None
    }

@action("cloud_init/template/detail/<template_id:int>")
@action.uses("cloud_init/template_detail.html", db)
def template_detail(template_id):
    """Cloud-init template detail page"""
    
    template = db(db.cloud_init_templates.id == template_id).select().first()
    if not template:
        abort(404)
    
    # Parse template variables
    try:
        template_variables = json.loads(template.template_variables or '[]')
    except:
        template_variables = []
    
    # Get sample configuration
    processor = CloudInitProcessor()
    sample_config = processor.generate_sample_config(template_variables)
    
    # Get deployment history
    deployments = db(db.deployment_jobs.cloud_init_template_id == template_id).select(
        orderby=~db.deployment_jobs.created_on,
        limitby=(0, 10)
    )
    
    return {
        'template': template,
        'template_variables': template_variables,
        'sample_config': sample_config,
        'deployments': deployments
    }

@action("cloud_init/template/preview/<template_id:int>", methods=['GET', 'POST'])
@action.uses("cloud_init/template_preview.html", db)
def template_preview(template_id):
    """Preview rendered cloud-init template"""
    
    template = db(db.cloud_init_templates.id == template_id).select().first()
    if not template:
        abort(404)
    
    # Parse template variables
    try:
        template_variables = json.loads(template.template_variables or '[]')
        default_variables = template.default_variables or {}
    except:
        template_variables = []
        default_variables = {}
    
    processor = CloudInitProcessor()
    rendered_content = None
    validation_errors = []
    variables_form = None
    
    if request.method == 'POST':
        # Get variables from form
        variables = {}
        for var in template_variables:
            value = request.forms.get(f'var_{var}', '')
            if value:
                # Try to parse as JSON for complex types
                try:
                    if value.startswith('[') or value.startswith('{'):
                        variables[var] = json.loads(value)
                    else:
                        variables[var] = value
                except:
                    variables[var] = value
            elif var in default_variables:
                variables[var] = default_variables[var]
        
        try:
            # Render template
            rendered_content = processor.render_template(template.template_content, variables)
            
            # Validate rendered content
            validator = CloudInitValidator()
            is_valid, errors, parsed_config = validator.validate_rendered_config(rendered_content)
            
            if not is_valid:
                validation_errors = errors
            
        except TemplateRenderError as e:
            validation_errors = [f"Template rendering failed: {str(e)}"]
    
    # Create form for variables
    form_fields = []
    for var in template_variables:
        default_value = default_variables.get(var, '')
        if isinstance(default_value, (list, dict)):
            default_value = json.dumps(default_value)
        
        form_fields.append({
            'name': var,
            'label': var.replace('_', ' ').title(),
            'value': request.forms.get(f'var_{var}', str(default_value)),
            'type': 'textarea' if isinstance(default_variables.get(var), (list, dict)) else 'text'
        })
    
    return {
        'template': template,
        'template_variables': template_variables,
        'form_fields': form_fields,
        'rendered_content': rendered_content,
        'validation_errors': validation_errors
    }

@action("cloud_init/template/clone/<template_id:int>")
@action.uses(db)
def template_clone(template_id):
    """Clone an existing template"""
    
    template = db(db.cloud_init_templates.id == template_id).select().first()
    if not template:
        abort(404)
    
    # Create cloned template
    cloned_data = {
        'name': f"{template.name} (Copy)",
        'description': template.description,
        'category': template.category,
        'template_content': template.template_content,
        'default_variables': template.default_variables,
        'template_variables': template.template_variables,
        'is_active': False,  # Set inactive by default
        'created_on': datetime.utcnow(),
        'updated_on': datetime.utcnow()
    }
    
    new_template_id = db.cloud_init_templates.insert(**cloned_data)
    db.commit()
    
    logger.info(f"Cloned template {template.name} as new template {new_template_id}")
    redirect(URL('cloud_init/template/edit', new_template_id))

@action("cloud_init/template/delete/<template_id:int>")
@action.uses(db)
def template_delete(template_id):
    """Delete a cloud-init template"""
    
    template = db(db.cloud_init_templates.id == template_id).select().first()
    if not template:
        response.status = 404
        return {'success': False, 'error': 'Template not found'}
    
    # Check if template is used in any deployments
    deployment_count = db(db.deployment_jobs.cloud_init_template_id == template_id).count()
    
    if deployment_count > 0:
        response.status = 400
        return {
            'success': False,
            'error': f'Cannot delete template. It is used in {deployment_count} deployment(s).'
        }
    
    try:
        # Delete template
        db(db.cloud_init_templates.id == template_id).delete()
        db.commit()
        
        logger.info(f"Deleted cloud-init template: {template.name}")
        
        return {'success': True, 'message': 'Template deleted successfully'}
        
    except Exception as e:
        logger.error(f"Failed to delete template: {e}")
        response.status = 500
        return {'success': False, 'error': str(e)}

@action("api/cloud_init/validate", methods=['POST'])
@action.uses(db)
def api_validate_template():
    """API endpoint to validate cloud-init template"""
    
    try:
        data = request.json
        if not data or 'template_content' not in data:
            response.status = 400
            return {'success': False, 'error': 'Missing template_content'}
        
        processor = CloudInitProcessor()
        is_valid, errors = processor.validate_template(data['template_content'])
        
        # Extract variables
        variables = processor.extract_variables(data['template_content'])
        
        return {
            'success': True,
            'valid': is_valid,
            'errors': errors,
            'variables': variables
        }
        
    except Exception as e:
        logger.error(f"Template validation API error: {e}")
        response.status = 500
        return {'success': False, 'error': str(e)}

@action("api/cloud_init/render", methods=['POST'])
@action.uses(db)
def api_render_template():
    """API endpoint to render cloud-init template with variables"""
    
    try:
        data = request.json
        if not data or 'template_content' not in data:
            response.status = 400
            return {'success': False, 'error': 'Missing template_content'}
        
        template_content = data['template_content']
        variables = data.get('variables', {})
        
        processor = CloudInitProcessor()
        
        try:
            rendered = processor.render_template(template_content, variables)
            
            # Validate rendered content
            validator = CloudInitValidator()
            is_valid, errors, parsed_config = validator.validate_rendered_config(rendered)
            
            return {
                'success': True,
                'rendered_content': rendered,
                'valid': is_valid,
                'errors': errors
            }
            
        except TemplateRenderError as e:
            return {
                'success': False,
                'error': str(e)
            }
    
    except Exception as e:
        logger.error(f"Template render API error: {e}")
        response.status = 500
        return {'success': False, 'error': str(e)}

@action("cloud_init/library")
@action.uses("cloud_init/library.html", db)
def template_library():
    """Template library with pre-built templates"""
    
    # Define template library
    library_templates = [
        {
            'name': 'Ubuntu Server Basic',
            'description': 'Basic Ubuntu server setup with user creation and SSH keys',
            'category': 'Base',
            'template_content': '''#cloud-config
hostname: {{ hostname }}
fqdn: {{ hostname }}.{{ domain }}

users:
  - name: {{ username }}
    groups: [adm, cdrom, sudo, dip, plugdev, lxd]
    lock_passwd: false
    passwd: {{ password | sha256_crypt }}
    shell: /bin/bash
    ssh_authorized_keys:
      - {{ ssh_key }}

ssh_pwauth: false
disable_root: true

package_update: true
package_upgrade: true

packages:
  - curl
  - wget
  - htop
  - vim
  - git
  - unzip

timezone: {{ timezone }}
locale: {{ locale }}

runcmd:
  - systemctl enable ssh
  - systemctl start ssh
''',
            'variables': ['hostname', 'domain', 'username', 'password', 'ssh_key', 'timezone', 'locale']
        },
        {
            'name': 'Docker Host',
            'description': 'Ubuntu server with Docker and Docker Compose installed',
            'category': 'Container',
            'template_content': '''#cloud-config
hostname: {{ hostname }}
fqdn: {{ hostname }}.{{ domain }}

users:
  - name: {{ username }}
    groups: [adm, cdrom, sudo, dip, plugdev, lxd, docker]
    lock_passwd: false
    passwd: {{ password | sha256_crypt }}
    shell: /bin/bash
    ssh_authorized_keys:
      - {{ ssh_key }}

ssh_pwauth: false
disable_root: true

package_update: true
package_upgrade: true

packages:
  - apt-transport-https
  - ca-certificates
  - curl
  - gnupg
  - lsb-release

runcmd:
  # Install Docker
  - curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
  - echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list
  - apt-get update
  - apt-get install -y docker-ce={{ docker_version }}* docker-ce-cli={{ docker_version }}* containerd.io
  - systemctl enable docker
  - systemctl start docker
  # Install Docker Compose
  - curl -L "https://github.com/docker/compose/releases/download/{{ compose_version }}/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
  - chmod +x /usr/local/bin/docker-compose
  - ln -s /usr/local/bin/docker-compose /usr/bin/docker-compose

timezone: {{ timezone }}
''',
            'variables': ['hostname', 'domain', 'username', 'password', 'ssh_key', 'timezone', 'docker_version', 'compose_version']
        },
        {
            'name': 'Kubernetes Worker',
            'description': 'Ubuntu server configured as Kubernetes worker node',
            'category': 'Kubernetes',
            'template_content': '''#cloud-config
hostname: {{ hostname }}
fqdn: {{ hostname }}.{{ domain }}

users:
  - name: {{ username }}
    groups: [adm, cdrom, sudo, dip, plugdev, lxd]
    lock_passwd: false
    passwd: {{ password | sha256_crypt }}
    shell: /bin/bash
    ssh_authorized_keys:
      - {{ ssh_key }}

ssh_pwauth: false
disable_root: true

package_update: true
package_upgrade: true

packages:
  - apt-transport-https
  - ca-certificates
  - curl
  - socat
  - conntrack

write_files:
  - path: /etc/modules-load.d/k8s.conf
    content: |
      overlay
      br_netfilter
  - path: /etc/sysctl.d/k8s.conf
    content: |
      net.bridge.bridge-nf-call-iptables  = 1
      net.bridge.bridge-nf-call-ip6tables = 1
      net.ipv4.ip_forward                 = 1

runcmd:
  # Load kernel modules
  - modprobe overlay
  - modprobe br_netfilter
  - sysctl --system
  # Install containerd
  - curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
  - echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list
  - apt-get update
  - apt-get install -y containerd.io
  - mkdir -p /etc/containerd
  - containerd config default | tee /etc/containerd/config.toml
  - systemctl restart containerd
  - systemctl enable containerd
  # Install kubeadm, kubelet, kubectl
  - curl -fsSLo /usr/share/keyrings/kubernetes-archive-keyring.gpg https://packages.cloud.google.com/apt/doc/apt-key.gpg
  - echo "deb [signed-by=/usr/share/keyrings/kubernetes-archive-keyring.gpg] https://apt.kubernetes.io/ kubernetes-xenial main" | tee /etc/apt/sources.list.d/kubernetes.list
  - apt-get update
  - apt-get install -y kubelet={{ kubernetes_version }}* kubeadm={{ kubernetes_version }}* kubectl={{ kubernetes_version }}*
  - apt-mark hold kubelet kubeadm kubectl
  - systemctl enable kubelet

timezone: {{ timezone }}
''',
            'variables': ['hostname', 'domain', 'username', 'password', 'ssh_key', 'timezone', 'kubernetes_version']
        }
    ]
    
    return {
        'library_templates': library_templates
    }

@action("cloud_init/library/import", methods=['POST'])
@action.uses(db)
def import_library_template():
    """Import a template from the library"""
    
    try:
        data = request.json
        if not data:
            response.status = 400
            return {'success': False, 'error': 'No data provided'}
        
        # Create template from library data
        processor = CloudInitProcessor()
        
        template_data = {
            'name': data['name'],
            'description': data['description'],
            'category': data['category'],
            'template_content': data['template_content'],
            'template_variables': json.dumps(data['variables']),
            'default_variables': processor.generate_sample_config(data['variables']),
            'is_active': True,
            'created_on': datetime.utcnow(),
            'updated_on': datetime.utcnow()
        }
        
        template_id = db.cloud_init_templates.insert(**template_data)
        db.commit()
        
        logger.info(f"Imported library template: {data['name']}")
        
        return {
            'success': True,
            'template_id': template_id,
            'message': 'Template imported successfully'
        }
        
    except Exception as e:
        logger.error(f"Failed to import library template: {e}")
        response.status = 500
        return {'success': False, 'error': str(e)}