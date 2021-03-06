# Make sure that unpackaged files are noticed
%define _unpackaged_files_terminate_build      1

# Skip over compile errors in python3 files
%global _python_bytecompile_errors_terminate_build 0

# Don't create a debug package
%define debug_package %{nil}

%define install_base /usr/lib/esmond
%define config_base /etc/esmond
%define init_script_1 espolld
%define init_script_2 espersistd
 
Name:           esmond
Version:        2.0.3       
Release:        1%{?dist}
Summary:        esmond
Group:          Development/Libraries
License:        New BSD License 
URL:            http://software.es.net/esmond
Source0:        %{name}-%{version}.tar.gz
BuildRoot:      %{_tmppath}/%{name}-%{version}-%{release}-root-%(%{__id_u} -n)
AutoReqProv:    no

%if 0%{?el7}
BuildRequires:  python
BuildRequires:  python-virtualenv
%else
BuildRequires:  python27
%endif
BuildRequires:  httpd
BuildRequires:  postgresql-devel
BuildRequires:  mercurial
BuildRequires:  gcc

%if 0%{?el7}
Requires:       python
Requires:       python-virtualenv
Requires:       python2-mock
Requires:       mod_wsgi
%else
Requires:       python27
Requires:       python27-mod_wsgi
Requires:       python-mock
%endif
Requires:       cassandra20
Requires:       httpd
Requires:       postgresql
Requires:       postgresql-server
Requires:       postgresql-devel
Requires:       sqlite
Requires:       sqlite-devel
Requires:       memcached
#java 1.7 needed for cassandra. dependency wrong in cassandra rpm.
Requires:       java-1.7.0-openjdk


%description
Esmond is a system for collecting and storing large sets of time-series data. Esmond
uses a hybrid model for storing data using TSDB for time series data and an SQL
database for everything else. All data is available via a REST style interface
(as JSON) allowing for easy integration with other tools.

%pre
# Create the 'esmond' user
/usr/sbin/groupadd esmond 2> /dev/null || :
/usr/sbin/useradd -g esmond -r -s /sbin/nologin -c "Esmond User" -d /tmp esmond 2> /dev/null || :

%prep
%setup -q -n %{name}-%{version}

%build

%install
# Copy and build in place so that we know what the path in the various files
# will be
rm -rf %{buildroot}/*
mkdir -p %{buildroot}/%{config_base}
mkdir -p %{buildroot}/%{install_base}
cp -Ra . %{buildroot}/%{install_base}
cd %{buildroot}/%{install_base}

# Get rid of any remnants of the buildroot directory
find %{buildroot}/%{install_base} -type f -exec sed -i "s|%{buildroot}||" {} \;

#Create bin directory. virtualenv files will leave here.
mkdir -p %{buildroot}/%{install_base}/bin/

#Move the init scripts into place
%if 0%{?el7}
%else
mkdir -p %{buildroot}/etc/init.d
mv %{buildroot}/%{install_base}/rpm/init_scripts/%{init_script_1} %{buildroot}/etc/init.d/%{init_script_1}
mv %{buildroot}/%{install_base}/rpm/init_scripts/%{init_script_2} %{buildroot}/etc/init.d/%{init_script_2}
%endif

# Move the default RPM esmond.conf into place
mv %{buildroot}/%{install_base}/rpm/config_files/esmond.conf %{buildroot}/%{config_base}/esmond.conf

# Move the config script into place
mv %{buildroot}/%{install_base}/rpm/scripts/configure_esmond %{buildroot}/%{install_base}/configure_esmond

# Move the default settings.py into place
mv %{buildroot}/%{install_base}/rpm/config_files/settings.py %{buildroot}/%{install_base}/esmond/settings.py

# Move the apache configuration into place
mkdir -p %{buildroot}/etc/httpd/conf.d/
mv %{buildroot}/%{install_base}/rpm/config_files/apache-esmond.conf %{buildroot}/etc/httpd/conf.d/apache-esmond.conf

# ENV files
mkdir -p %{buildroot}/etc/profile.d
mv %{buildroot}/%{install_base}/rpm/config_files/esmond.csh %{buildroot}/etc/profile.d/esmond.csh
mv %{buildroot}/%{install_base}/rpm/config_files/esmond.sh %{buildroot}/etc/profile.d/esmond.sh

# Get rid of the 'rpm' directory now that all the files have been moved into place
rm -rf %{buildroot}/%{install_base}/rpm

# Install python libs so don't rely on pip connectivity during RPM install
# NOTE: This part is why its not noarch
cd %{buildroot}/%{install_base}
rm -f .gitignore
%if 0%{?el7}
virtualenv --prompt="(esmond)" .
%else
source /opt/rh/python27/enable
/opt/rh/python27/root/usr/bin/virtualenv --prompt="(esmond)" .
%endif
. bin/activate
#Invoking pip using 'python -m pip' to avoid 128 char shebang line limit that pip can hit in build envs like Jenkins
python -m pip install --install-option="--prefix=%{buildroot}%{install_base}" -r requirements.txt
# Need this for the 1.0->2.0 API key migration script
python -m pip install --install-option="--prefix=%{buildroot}%{install_base}" django-tastypie
#not pretty but below is the best way I could find to remove references to buildroot
find bin -type f -exec sed -i "s|%{buildroot}%{install_base}|%{install_base}|g" {} \;
find lib -type f -exec sed -i "s|%{buildroot}%{install_base}|%{install_base}|g" {} \;

%clean
[ "%{buildroot}" != "/" ] && rm -rf %{buildroot}

%post
cd %{install_base}
%if 0%{?el7}
%else
source /opt/rh/python27/enable
/opt/rh/python27/root/usr/bin/virtualenv --prompt="(esmond)" .
%endif
. bin/activate

#generate secret key
grep -q "SECRET_KEY =" esmond/settings.py || python util/gen_django_secret_key.py >> esmond/settings.py

# Create the logging directories
mkdir -p /var/log/esmond
mkdir -p /var/log/esmond/crashlog
touch /var/log/esmond/esmond.log
touch /var/log/esmond/django.log
touch /var/log/esmond/install.log
chown -R apache:apache /var/log/esmond
%if 0%{?el7}
chcon -R system_u:object_r:httpd_log_t:s0 /var/log/esmond
setsebool -P httpd_can_network_connect on
%endif

#handle updates
if [ "$1" = "2" ]; then
    #migrate pre-2.0 files
    if [ -e "/opt/esmond/esmond.conf" ]; then
        mv %{config_base}/esmond.conf %{config_base}/esmond.conf.default
        mv /opt/esmond/esmond.conf %{config_base}/esmond.conf
    elif [ -e "/opt/esmond/esmond.conf.rpmsave" ]; then
        mv %{config_base}/esmond.conf %{config_base}/esmond.conf.default
        mv /opt/esmond/esmond.conf.rpmsave %{config_base}/esmond.conf
    fi
fi

#run config script
chmod 755 configure_esmond
./configure_esmond $1

mkdir -p tsdb-data
touch tsdb-data/TSDB

# Create the TSDB directory
mkdir -p /var/lib/esmond
touch /var/lib/esmond/TSDB
chown -R esmond:esmond /var/lib/esmond

# Create the 'run' directory
mkdir -p /var/run/esmond
chown -R esmond:esmond /var/run/esmond

#fix any file permissions the pip packages mess-up 
find %{install_base}/lib -type f -perm 0666 -exec chmod 644 {} \;
 
%postun
if [ "$1" != "0" ]; then
    # An RPM upgrade
    /etc/init.d/httpd restart
fi

%files
%defattr(0644,esmond,esmond,0755)
%config(noreplace) %{config_base}/esmond.conf
%config %{install_base}/esmond/settings.py
%attr(0755,esmond,esmond) %{install_base}/bin/*
%attr(0755,esmond,esmond) %{install_base}/util/*
%attr(0755,esmond,esmond) %{install_base}/esmond_client/clients/*
%attr(0755,esmond,esmond) %{install_base}/mkdevenv
%attr(0755,esmond,esmond) %{install_base}/configure_esmond
%{install_base}/*
/etc/httpd/conf.d/apache-esmond.conf
%attr(0755,esmond,esmond) /etc/profile.d/esmond.csh
%attr(0755,esmond,esmond) /etc/profile.d/esmond.sh
%if 0%{?el7}
%else
%attr(0755,esmond,esmond) /etc/init.d/%{init_script_1}
%attr(0755,esmond,esmond) /etc/init.d/%{init_script_2}
%endif
%changelog
* Wed Mar 5 2014 Monte Goode <mmgoode@lbl.gov> .99-1
- Initial Esmond Spec File including perfsonar support

* Wed Apr 27 2011 Aaron Brown <aaron@internet2.edu> 1.0-1
- Initial Esmond Spec File
